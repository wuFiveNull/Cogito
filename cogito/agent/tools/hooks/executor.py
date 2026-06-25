# cogito/agent/tools/hooks/executor.py
#
# HookExecutor — orchestrates pre/post tool execution hooks.
#
# Reference: akashic-agent ToolExecutor pattern.
#
# Integration into orchestrator:
#   pre-tool:   Before Step 7 (Policy Evaluation).
#   post-tool:  After Step 19 (Return ToolResult).

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from cogito.agent.tools.hooks.base import (
    HookContext,
    HookDecision,
    HookEvent,
    HookOutcome,
    HookTraceItem,
    ToolHook,
)

logger = logging.getLogger(__name__)


class HookExecutor:
    """Orchestrates pre/post tool execution hooks.

    Routes:
      pre_tool_use:     Any hook can deny or modify arguments.
      post_tool_use:    Hooks can augment the result.
      post_tool_error:  Hooks can augment the error.
    """

    def __init__(self, hooks: Sequence[ToolHook] | None = None) -> None:
        self._hooks: list[ToolHook] = list(hooks) if hooks else []

    def add_hook(self, hook: ToolHook) -> None:
        """Register a hook."""
        self._hooks.append(hook)

    def remove_hook(self, name: str) -> None:
        """Remove a hook by name."""
        self._hooks[:] = [h for h in self._hooks if h.name != name]

    # ── Execution ───────────────────────────────────────────────────

    async def run_pre_hooks(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        definition: Mapping[str, Any] | None = None,
        context: Mapping[str, object] | None = None,
    ) -> tuple[HookOutcome | None, list[HookTraceItem], dict[str, Any]]:
        """Run pre-tool-use hooks.

        Returns (deny_outcome, trace, final_arguments).
        If any hook denies, the first denial outcome is returned.
        """
        trace: list[HookTraceItem] = []
        current_args = dict(arguments)

        for hook in self._hooks:
            if hook.event != "pre_tool_use":
                continue

            ctx = HookContext(
                event="pre_tool_use",
                tool_name=tool_name,
                arguments=current_args,
                definition=dict(definition or {}),
                context=context or {},
            )

            if not hook.matches(ctx):
                trace.append(HookTraceItem(
                    hook_name=hook.name,
                    event="pre_tool_use",
                    matched=False,
                ))
                continue

            try:
                outcome = await hook.run(ctx)
            except Exception as exc:
                logger.warning("Hook %s pre-run failed: %s", hook.name, exc)
                trace.append(HookTraceItem(
                    hook_name=hook.name,
                    event="pre_tool_use",
                    matched=True,
                    decision="pass",
                    reason=f"Hook error: {exc}",
                ))
                continue

            trace.append(HookTraceItem(
                hook_name=hook.name,
                event="pre_tool_use",
                matched=True,
                decision=outcome.decision,
                reason=outcome.reason,
            ))

            if outcome.decision == "deny":
                return outcome, trace, current_args

            if outcome.updated_input is not None:
                current_args.update(outcome.updated_input)

        return None, trace, current_args

    async def run_post_hooks(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        raw_result: Any,
        definition: Mapping[str, Any] | None = None,
        context: Mapping[str, object] | None = None,
    ) -> tuple[list[HookTraceItem], list[str]]:
        """Run post-tool-use hooks.

        Returns (trace, extra_messages) where extra_messages are strings
        to append to the tool result.
        """
        trace: list[HookTraceItem] = []
        extra_messages: list[str] = []

        for hook in self._hooks:
            if hook.event != "post_tool_use":
                continue

            ctx = HookContext(
                event="post_tool_use",
                tool_name=tool_name,
                arguments=dict(arguments),
                definition=dict(definition or {}),
                context=context or {},
                raw_result=raw_result,
            )

            if not hook.matches(ctx):
                trace.append(HookTraceItem(
                    hook_name=hook.name,
                    event="post_tool_use",
                    matched=False,
                ))
                continue

            try:
                outcome = await hook.run(ctx)
            except Exception as exc:
                logger.warning("Hook %s post-run failed: %s", hook.name, exc)
                continue

            trace.append(HookTraceItem(
                hook_name=hook.name,
                event="post_tool_use",
                matched=True,
                decision=outcome.decision,
                reason=outcome.reason,
                extra_message=outcome.extra_message,
            ))
            if outcome.extra_message:
                extra_messages.append(outcome.extra_message)

        return trace, extra_messages

    async def run_error_hooks(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        error: str,
        definition: Mapping[str, Any] | None = None,
        context: Mapping[str, object] | None = None,
    ) -> list[HookTraceItem]:
        """Run post-tool-error hooks."""
        trace: list[HookTraceItem] = []

        for hook in self._hooks:
            if hook.event != "post_tool_error":
                continue

            ctx = HookContext(
                event="post_tool_error",
                tool_name=tool_name,
                arguments=dict(arguments),
                definition=dict(definition or {}),
                context=context or {},
                error=error,
            )

            if not hook.matches(ctx):
                continue

            try:
                await hook.run(ctx)
            except Exception as exc:
                logger.warning("Hook %s error-run failed: %s", hook.name, exc)

            trace.append(HookTraceItem(
                hook_name=hook.name,
                event="post_tool_error",
                matched=True,
            ))

        return trace
