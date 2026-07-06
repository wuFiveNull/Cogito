# cogito/agent/subagent/manager.py
#
# SubAgentManager — manages sub-agent instances with independent LLM loops.
#
# A SubAgent is a self-contained agent with:
#   - A fixed subset of tools (cannot add tools at runtime)
#   - An independent LLM loop (model → tools → model → result)
#   - Iteration budget control (max rounds + warning thresholds)
#   - Result trimming for long runs
#
# Reference: akashic-agent subagent.py

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from cogito.agent.subagent.spec import SubAgentProfile, SubAgentResult, SubAgentSpec, SubAgentStatus

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

_WARN_THRESHOLD = 5           # Remaining iterations <= this triggers warning
_MAX_TOOL_RESULT_CHARS = 50_000
_RECENT_TOOL_ROUNDS = 3       # Keep full tool results for recent N rounds
_CLEARED = "[cleared]"        # Placeholder for old tool results
_CLEANUP_THRESHOLD = 1        # Force cleanup when remaining ≈ this
_REFLECT_PROMPT = (
    "Based on the tool results above, decide the next action.\n"
    "If the task is complete, output the final result directly."
)
_REFLECT_PROMPT_WARN = (
    "Based on the tool results above, decide the next action.\n"
    "⚠️ Only {remaining} steps remaining. Prioritise core objectives.\n"
    "If the task is complete, output the final result directly."
)
_REFLECT_PROMPT_LAST = (
    "⚠️ The next step will be your last. Focus on producing the final result."
)
_CLEANUP_PROMPT = (
    "The step budget is exhausted. Enter forced finalisation.\n"
    "You must produce a summary of what was accomplished and what remains."
)


# ── SubAgent Manager ───────────────────────────────────────────────────

class SubAgentManager:
    """Manages sub-agent lifecycle: create, run, and collect results.

    Each sub-agent gets its own message loop, tool set, and iteration
    budget.  Sub-agents share the parent's LLM provider but operate
    with a restricted tool view.
    """

    def __init__(
        self,
        llm_provider: object | None = None,
        registry: object | None = None,
    ) -> None:
        self._llm_provider = llm_provider
        self._registry = registry
        self._agents: dict[str, asyncio.Task] = {}
        self._results: dict[str, SubAgentResult] = {}

    async def spawn(
        self,
        spec: SubAgentSpec,
    ) -> str:
        """Create and start a sub-agent. Returns agent_id immediately."""
        agent_id = uuid4().hex[:12]

        async def run_agent():
            try:
                result = await self._run(spec, agent_id)
                self._results[agent_id] = result
            except Exception as exc:
                self._results[agent_id] = SubAgentResult(
                    agent_id=agent_id,
                    status="error",
                    exit_reason="exception",
                    summary=str(exc),
                    iteration_count=0,
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    error=str(exc),
                )

        self._agents[agent_id] = asyncio.create_task(run_agent())
        return agent_id

    async def get_result(self, agent_id: str, timeout: float | None = None) -> SubAgentResult | None:
        """Get a sub-agent's result. Blocks until complete if still running.

        Returns None if the agent_id does not exist.
        """
        if agent_id not in self._agents:
            return self._results.get(agent_id)

        try:
            await asyncio.wait_for(self._agents[agent_id], timeout=timeout)
        except asyncio.TimeoutError:
            return None

        return self._results.get(agent_id)

    async def cancel(self, agent_id: str) -> bool:
        """Cancel a running sub-agent. Returns True if cancelled."""
        task = self._agents.get(agent_id)
        if task is None or task.done():
            return False
        task.cancel()
        self._results[agent_id] = SubAgentResult(
            agent_id=agent_id,
            status="incomplete",
            exit_reason="cancelled",
            summary="Sub-agent was cancelled",
            iteration_count=0,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        return True

    async def list_active(self) -> list[dict]:
        """List all active (running) sub-agents."""
        active = []
        for aid, task in self._agents.items():
            if not task.done():
                active.append({"agent_id": aid, "status": "running"})
        return active

    # ── Internal: sub-agent loop ────────────────────────────────────

    async def _run(self, spec: SubAgentSpec, agent_id: str) -> SubAgentResult:
        """Run the sub-agent's independent LLM loop."""
        if self._llm_provider is None:
            return SubAgentResult(
                agent_id=agent_id, status="error", exit_reason="no_provider",
                summary="LLM provider not configured", iteration_count=0,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                error="LLM provider not configured",
            )

        started_at = datetime.now(timezone.utc)

        # Build system prompt
        system_prompt = self._build_system_prompt(spec)

        # Build message list
        messages = [{"role": "system", "content": system_prompt}]
        # Use a simpler message format if the provider expects it
        messages.append({"role": "user", "content": spec.task})

        iterations = 0
        exit_reason = "incomplete"
        summary = ""
        tool_defs = self._get_tool_definitions(spec.tool_names)

        while iterations < spec.max_iterations:
            iterations += 1
            remaining = spec.max_iterations - iterations

            # Check wall-clock timeout
            if (datetime.now(timezone.utc) - started_at).total_seconds() > spec.timeout_seconds:
                exit_reason = "timeout"
                summary = "Sub-agent timed out"
                break

            # LLM call: get response with tool calls
            try:
                response = await self._llm_call(messages, tool_defs, remaining)
            except Exception as exc:
                exit_reason = "error"
                summary = f"LLM call failed: {exc}"
                break

            # Extract content and tool calls
            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            # If no tool calls, the agent is done
            if not tool_calls:
                summary = content or "Task completed"
                exit_reason = "completed"
                break

            # Add assistant message
            assistant_msg = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
            messages.append(assistant_msg)

            # Execute tool calls
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_args = tc.get("arguments", {})
                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except json.JSONDecodeError:
                        tool_args = {}
                tool_result = await self._execute_tool(tool_name, tool_args, spec)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_result[: _MAX_TOOL_RESULT_CHARS],
                })

            # Trim old tool results
            if remaining <= _CLEANUP_THRESHOLD:
                self._trim_tool_results(messages)

            # Handle forced finalisation
            if remaining <= 0:
                exit_reason = "budget_exhausted"
                summary = await self._forced_finalise(messages)
                break

        finished_at = datetime.now(timezone.utc)

        return SubAgentResult(
            agent_id=agent_id,
            status="completed" if exit_reason == "completed" else "incomplete",
            exit_reason=exit_reason,
            summary=summary or "No result produced",
            iteration_count=iterations,
            started_at=started_at,
            finished_at=finished_at,
        )

    # ── Internal helpers ────────────────────────────────────────────

    def _build_system_prompt(self, spec: SubAgentSpec) -> str:
        """Build the system prompt for a sub-agent."""
        base = (
            "You are a sub-agent assistant. You have access to a limited set of tools.\n"
            "Analyse the user's request, use tools as needed, and produce a final answer.\n"
            "When you have enough information, respond directly without calling tools.\n"
        )
        if spec.system_prompt_extra:
            base += f"\n{spec.system_prompt_extra}\n"
        return base

    def _get_tool_definitions(self, tool_names: tuple[str, ...]) -> list[dict]:
        """Get tool definitions for the allowed tool set."""
        if self._registry is None or not hasattr(self._registry, "snapshot"):
            return []
        try:
            snapshot = self._registry.snapshot()
            defs = []
            for name in tool_names:
                if name in snapshot.definitions:
                    defn = snapshot.definitions[name]
                    defs.append({
                        "name": defn.name,
                        "description": defn.description,
                        "input_schema": defn.input_schema,
                    })
            return defs
        except Exception:
            return []

    async def _llm_call(
        self,
        messages: list[dict[str, Any]],
        tool_defs: list[dict],
        remaining: int,
    ) -> dict[str, Any]:
        """Make an LLM call with the provider.

        Returns a dict with 'content' and 'tool_calls'.
        """
        # Build reflect prompt
        if remaining <= 0:
            extra = _REFLECT_PROMPT_LAST
        elif remaining <= _WARN_THRESHOLD:
            extra = _REFLECT_PROMPT_WARN.format(remaining=remaining)
        else:
            extra = _REFLECT_PROMPT

        # Append reflect message
        call_messages = list(messages)
        call_messages.append({"role": "user", "content": extra})

        if hasattr(self._llm_provider, "chat_completion"):
            kwargs = {"messages": call_messages}
            if tool_defs:
                kwargs["tools"] = tool_defs
            response = await self._llm_provider.chat_completion(**kwargs)

            msg = response.get("message", {})
            return {
                "content": msg.get("content", ""),
                "tool_calls": [
                    {
                        "id": tc.get("id", f"call_{i}"),
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", "{}"),
                    }
                    for i, tc in enumerate(msg.get("tool_calls", []))
                ],
            }
        else:
            # Fallback: direct string return
            result = str(self._llm_provider)
            return {"content": result, "tool_calls": []}

    async def _execute_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        spec: SubAgentSpec,
    ) -> str:
        """Execute a single tool call for the sub-agent."""
        if self._registry is None:
            return "Error: tool registry not available"

        try:
            snapshot = self._registry.snapshot()
            handler = snapshot.handlers.get(tool_name)
            if handler is None:
                return f"Error: tool '{tool_name}' not found"

            from cogito.agent.ports.tools.executor import ToolExecutionContext
            ctx = ToolExecutionContext(
                turn_id="subagent",
                request_id="subagent",
                session_id="subagent",
                actor_id="subagent",
                call_id=f"sub_{uuid4().hex[:8]}",
            )
            result = await handler.execute(arguments=tool_args, context={})

            if isinstance(result, dict):
                if "error" in result:
                    err = result["error"]
                    return f"Error: {err.get('message', str(err))}"
                content = result.get("content", result.get("stdout", result.get("output", str(result))))
                if isinstance(content, (list, dict)):
                    return json.dumps(content, ensure_ascii=False)[:_MAX_TOOL_RESULT_CHARS]
                return str(content)[:_MAX_TOOL_RESULT_CHARS]

            return str(result)[:_MAX_TOOL_RESULT_CHARS]

        except Exception as exc:
            return f"Error executing {tool_name}: {exc}"

    @staticmethod
    def _trim_tool_results(messages: list[dict[str, Any]]) -> None:
        """Replace old tool results with placeholder to save context.

        Keeps full results for the last _RECENT_TOOL_ROUNDS rounds of
        tool calls; older rounds get their tool results replaced.
        """
        tool_round_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        if len(tool_round_indices) <= _RECENT_TOOL_ROUNDS:
            return

        cutoff = tool_round_indices[-_RECENT_TOOL_ROUNDS]
        for i in range(cutoff):
            if messages[i].get("role") == "tool":
                messages[i] = {"role": "tool", "tool_call_id": messages[i].get("tool_call_id", ""),
                               "content": _CLEARED}

    async def _forced_finalise(self, messages: list[dict[str, Any]]) -> str:
        """When iteration budget exhausted, force a final summary from the LLM."""
        messages.append({"role": "user", "content": _CLEANUP_PROMPT})
        try:
            response = await self._llm_call(messages, [], 0)
            return response.get("content", "Budget exhausted — no summary produced")
        except Exception:
            return "Budget exhausted — forced finalisation failed"
