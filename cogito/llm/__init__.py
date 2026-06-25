# cogito/llm/__init__.py

from .protocol import ChatProvider
from .request import ChatRequest, ChatMessage, TextContent, ImageContent, ToolCallRequest, ToolDefinition
from .response import LLMResponse, ToolCall, TokenUsage
from .stream import ContentDelta, ThinkingDelta, ToolCallDelta, UsageDelta, StreamCompleted, LLMStreamEvent
from .capabilities import ModelCapabilities, ModelProfile, validate_request_capabilities
from .errors import (
    LLMError,
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMConnectionError,
    ContentSafetyError,
    ContextLengthError,
    ModelCapabilityError,
    InvalidLLMResponseError,
)
from .embedding import Embedder, EmbeddingProfile


def __getattr__(name: str):
    """Lazy-import openai-dependent modules."""

    if name in ("ModelRegistry", "UnknownModelError"):
        from .registry import ModelRegistry as _r, UnknownModelError as _e
        globals()["ModelRegistry"] = _r
        globals()["UnknownModelError"] = _e
        return _r if name == "ModelRegistry" else _e

    if name in ("LLMService", "UnknownModelRoleError"):
        from .service import LLMService as _s, UnknownModelRoleError as _e
        globals()["LLMService"] = _s
        globals()["UnknownModelRoleError"] = _e
        return _s if name == "LLMService" else _e

    if name == "ChatBackend":
        from .backend import ChatBackend as _b
        globals()["ChatBackend"] = _b
        return _b

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ChatProvider",
    "ChatRequest",
    "ChatMessage",
    "TextContent",
    "ImageContent",
    "ToolCallRequest",
    "ToolDefinition",
    "LLMResponse",
    "ToolCall",
    "TokenUsage",
    "ContentDelta",
    "ThinkingDelta",
    "ToolCallDelta",
    "UsageDelta",
    "StreamCompleted",
    "LLMStreamEvent",
    "ModelCapabilities",
    "ModelProfile",
    "validate_request_capabilities",
    "LLMError",
    "LLMAuthenticationError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMConnectionError",
    "ContentSafetyError",
    "ContextLengthError",
    "ModelCapabilityError",
    "InvalidLLMResponseError",
    "ModelRegistry",
    "UnknownModelError",
    "LLMService",
    "UnknownModelRoleError",
    "ChatBackend",
    "Embedder",
    "EmbeddingProfile",
]
