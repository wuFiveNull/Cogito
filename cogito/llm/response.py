# cogito/llm/response.py

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str

    raw_arguments: str
    arguments: Mapping[str, Any] | None = None
    parse_error: str | None = None


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()

    thinking: str | None = None
    finish_reason: str | None = None

    model: str | None = None
    provider: str | None = None
    provider_response_id: str | None = None

    usage: TokenUsage | None = None
    provider_fields: Mapping[str, Any] = field(default_factory=dict)
