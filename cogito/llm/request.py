# cogito/llm/request.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence


@dataclass(frozen=True)
class TextContent:
    text: str


@dataclass(frozen=True)
class ImageContent:
    url: str
    detail: Literal["auto", "low", "high"] = "auto"


ContentPart = TextContent | ImageContent
MessageContent = str | Sequence[ContentPart]


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    raw_arguments: str


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: MessageContent | None = None

    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()

    tool_choice: str | Mapping[str, Any] | None = "auto"

    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: tuple[str, ...] = ()

    disable_thinking: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
