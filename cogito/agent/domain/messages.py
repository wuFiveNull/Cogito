# cogito/agent/domain/messages.py
#
# Strongly-typed message model for the Agent runtime.
#
# Each role has its own frozen dataclass so that pattern matching and
# static analysis can discriminate on the type at compile time.
#
# Design rules (see agent-loop-phase-spec §5.1):
#   - AssistantMessage must contain exactly one of {content, tool_calls}.
#   - ToolMessage.tool_call_id must correspond to a prior AssistantMessage.
#   - Provider-native objects live only inside adapters, never here.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, TypeAlias

from cogito.agent.domain.tools import ToolCall


# ── Role-specific message types ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SystemMessage:
    """Persistent system-level instruction (policy, rules)."""

    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserMessage:
    """End-user input for the current turn."""

    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """Model-generated response — text, tool calls, or both.

    Models may produce explanatory text before calling tools.
    Both content and tool_calls are allowed simultaneously.
    """

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    provider_response_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        has_text = bool(self.content and self.content.strip())
        has_tools = bool(self.tool_calls)
        if has_text and has_tools:
            # Allow both — model can explain before calling tools
            pass
        elif not has_text and not has_tools:
            raise ValueError(
                "AssistantMessage must have content or tool_calls",
            )


@dataclass(frozen=True, slots=True)
class ToolMessage:
    """Result of executing a single tool call."""

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)


# ── Discriminated union ─────────────────────────────────────────────────

ModelMessage: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolMessage
