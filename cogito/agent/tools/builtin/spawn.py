# cogito/agent/tools/builtin/spawn.py
#
# Built-in tools: spawn, spawn_output — delegate tasks to sub-agents.
#
# spawn:          Start a sub-agent with a task and tool set, return agent_id.
# spawn_output:   Poll a sub-agent's result.

from __future__ import annotations

import logging
from typing import Any, Mapping

from cogito.agent.domain.tools import (
    ToolConcurrencyMode,
    ToolDefinition,
    ToolKind,
    ToolLimits,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)
from cogito.agent.subagent.manager import SubAgentManager
from cogito.agent.subagent.spec import SubAgentSpec

logger = logging.getLogger(__name__)


class SpawnHandler:
    """Handler for spawn — delegates a task to a sub-agent."""

    def __init__(
        self,
        *,
        subagent_manager: SubAgentManager | None = None,
    ) -> None:
        self._manager = subagent_manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="spawn",
            description="Delegate a task to a sub-agent that runs independently. "
                        "Provide the task description and optionally limit which tools it can use.",
            input_schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "minLength": 1, "description": "Task description for the sub-agent"},
                    "tool_names": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Tool names the sub-agent can use (empty = all available)",
                    },
                    "max_iterations": {"type": "integer", "minimum": 1, "maximum": 50,
                                       "description": "Max tool-call rounds"},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.LOCAL_MUTATION,
            risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=30.0,
            idempotent=False,
            parallel_safe=False,
            kind=ToolKind.EXECUTE,
            risk=ToolRisk.LOCAL_WRITE,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_TOOL,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        if self._manager is None:
            return {"error": {"code": "SUBAGENT_NOT_CONFIGURED", "message": "Sub-agent system not configured"}}

        task = str(arguments.get("task", ""))
        tool_names = tuple(str(t) for t in arguments.get("tool_names", [])) if arguments.get("tool_names") else ()
        max_iterations = int(arguments.get("max_iterations", 10))

        spec = SubAgentSpec(
            task=task,
            tool_names=tool_names,
            max_iterations=max_iterations,
        )

        try:
            agent_id = await self._manager.spawn(spec)
            return {
                "agent_id": agent_id,
                "status": "started",
                "message": f"Sub-agent {agent_id} started. Use spawn_output(agent_id='{agent_id}') to get results.",
                "task": task,
            }
        except Exception as exc:
            return {"error": {"code": "SPAWN_FAILED", "message": str(exc)}}


class SpawnOutputHandler:
    """Handler for spawn_output — polls a sub-agent's result."""

    def __init__(
        self,
        *,
        subagent_manager: SubAgentManager | None = None,
    ) -> None:
        self._manager = subagent_manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="spawn_output",
            description="Get the result from a sub-agent started with spawn(). "
                        "If the sub-agent is still running, waits for completion (up to timeout).",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "minLength": 1, "description": "Agent ID from spawn()"},
                    "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 300,
                                        "description": "Max seconds to wait for completion"},
                },
                "required": ["agent_id"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=310.0,
            idempotent=True,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        if self._manager is None:
            return {"error": {"code": "SUBAGENT_NOT_CONFIGURED", "message": "Sub-agent system not configured"}}

        agent_id = str(arguments.get("agent_id", ""))
        timeout = int(arguments.get("timeout_seconds", 60))

        try:
            result = await self._manager.get_result(agent_id, timeout=float(timeout))
        except Exception as exc:
            return {"error": {"code": "SPAWN_OUTPUT_ERROR", "message": str(exc)}}

        if result is None:
            return {
                "agent_id": agent_id,
                "status": "running",
                "message": "Sub-agent is still running. Try again later or increase timeout.",
            }

        return {
            "agent_id": result.agent_id,
            "status": result.status.value if hasattr(result.status, "value") else result.status,
            "exit_reason": result.exit_reason,
            "summary": result.summary,
            "iteration_count": result.iteration_count,
            "started_at": result.started_at.isoformat(),
            "finished_at": result.finished_at.isoformat(),
            "error": result.error,
        }
