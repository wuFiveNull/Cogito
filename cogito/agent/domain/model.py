# cogito/agent/domain/model.py
#
# Model-call domain types for the Agent runtime.
#
# These live in the domain layer because AgentLoopPhase orchestrates
# model ↔ tool cycles without knowing any provider SDK.
#
# Design rules (see agent-loop-phase-spec §5.5–§5.7):
#   - All stream events are frozen dataclasses — no provider objects.
#   - ModelRoundOutput non-text XOR non-empty-tool-calls.
#   - ModelInvocationRequest contains everything the port needs.
#   - The ModelPort.stream() yields ModelStreamEvent instances only.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from cogito.agent.domain.messages import ModelMessage
from cogito.agent.domain.tools import ToolCall, ToolDefinition


# ── Finish reason ───────────────────────────────────────────────────────


class ModelFinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


# ── Model round mode ────────────────────────────────────────────────────


class ModelRoundMode(StrEnum):
    """Determined during stream assembly by the first meaningful event.

    UNKNOWN — no content yet.
    FINAL_RESPONSE — first non-empty text delta was seen.
    TOOL_CALLS — first tool-call delta was seen.
    """

    UNKNOWN = "unknown"
    FINAL_RESPONSE = "final_response"
    TOOL_CALLS = "tool_calls"


# ── Stream event types ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """A chunk of the final response text."""

    text: str


@dataclass(frozen=True, slots=True)
class ModelToolCallDelta:
    """A (partial) tool-call delta from the stream.

    ``ordinal`` is stable across deltas for the same call.
    Fields may arrive in separate events — the assembler merges them.
    """

    ordinal: int
    call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str = ""


@dataclass(frozen=True, slots=True)
class ModelUsageUpdate:
    """Partial or final token-usage information."""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelCompleted:
    """Model stream is done.  May carry a finish reason."""

    finish_reason: ModelFinishReason
    provider_response_id: str | None = None


ModelStreamEvent: TypeAlias = (
    ModelTextDelta | ModelToolCallDelta | ModelUsageUpdate | ModelCompleted
)


# ── Model invocation request ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelInvocationRequest:
    """Everything ModelPort needs to start one streamed generation.

    ``round_index`` is the zero-based model-call number within the turn.
    ``messages`` is the context-window-fitted view (not the canonical list).
    """

    turn_id: str
    request_id: str
    round_index: int
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    timeout_seconds: float
    max_output_tokens: int


# ── Model round output (after stream assembly) ──────────────────────────


@dataclass(frozen=True, slots=True)
class ModelRoundOutput:
    """Aggregated result of one model call.

    Exactly one of {text, tool_calls} is non-empty.
    """

    round_index: int
    finish_reason: ModelFinishReason
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    provider_response_id: str | None
    input_tokens: int
    output_tokens: int
