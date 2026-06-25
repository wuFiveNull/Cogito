# cogito/agent/tools/hooks/base.py
#
# ToolHook — abstract base for pre/post tool execution hooks.
#
# Reference: akashic-agent tool_hooks system.
#
# A hook intercepts tool execution at three points:
#   - pre_tool_use:   before execution (can modify arguments or deny)
#   - post_tool_use:  after successful execution (can augment result)
#   - post_tool_error: after execution failure (can augment error)

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


# ── Types ──────────────────────────────────────────────────────────────

HookEvent = Literal["pre_tool_use", "post_tool_use", "post_tool_error"]
HookDecision = Literal["pass", "deny"]


@dataclass(frozen=True, slots=True)
class HookContext:
    """Context passed to a hook's matches() and run() methods.

    Attributes:
        event:         Which event triggered this hook.
        tool_name:     Name of the tool being called.
        arguments:     Current tool arguments (pre-hooks can request changes).
        definition:    Serialisable tool definition dict (name, risk, etc.).
        context:       Execution context from orchestrator (turn_id, session_id, etc.).
        raw_result:    The raw result from tool execution (post hooks only).
        error:         Error message if execution failed (post_tool_error only).
    """
    event: HookEvent
    tool_name: str
    arguments: dict[str, Any]
    definition: dict[str, Any] = field(default_factory=dict)
    context: Mapping[str, object] = field(default_factory=dict)
    raw_result: Any = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """Result of a hook execution.

    Attributes:
        decision:      'pass' = allow, 'deny' = block execution.
        updated_input: If set, replaces the tool call arguments (pre-hook only).
        reason:        Human-readable reason for the decision.
        extra_message: Extra content appended to the tool result.
    """
    decision: HookDecision = "pass"
    updated_input: dict[str, Any] | None = None
    reason: str = ""
    extra_message: str = ""


@dataclass(frozen=True, slots=True)
class HookTraceItem:
    """Trace record for auditing hook execution."""
    hook_name: str
    event: HookEvent
    matched: bool
    decision: HookDecision = "pass"
    reason: str = ""
    extra_message: str = ""


# ── Abstract hook ──────────────────────────────────────────────────────

class ToolHook(ABC):
    """Abstract base for a single tool hook.

    Implement ``matches()`` to declare which tools/events trigger this
    hook, and ``run()`` to perform the check or transformation.
    """

    name: str = "unnamed_hook"
    event: HookEvent = "pre_tool_use"

    @abstractmethod
    def matches(self, ctx: HookContext) -> bool:
        """Return True when this hook should run for the given context."""
        ...

    @abstractmethod
    async def run(self, ctx: HookContext) -> HookOutcome:
        """Execute the hook and return an outcome.

        For pre-tool hooks, returning ``deny`` prevents execution.
        For post-tool hooks, ``extra_message`` can augment the result.
        """
        ...
