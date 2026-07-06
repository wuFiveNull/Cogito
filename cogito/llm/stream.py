# cogito/llm/stream.py

from dataclasses import dataclass

from .response import TokenUsage


@dataclass(frozen=True)
class ContentDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    call_id_delta: str | None = None
    name_delta: str | None = None
    arguments_delta: str | None = None


@dataclass(frozen=True)
class UsageDelta:
    usage: TokenUsage


@dataclass(frozen=True)
class StreamCompleted:
    finish_reason: str | None = None


LLMStreamEvent = (
    ContentDelta
    | ThinkingDelta
    | ToolCallDelta
    | UsageDelta
    | StreamCompleted
)
