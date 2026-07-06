"""Model 统一契约。

AGENT-COGNITION / 4. Model 与 Embedding Provider
MODEL-ADAPTER / 2. ModelRequest
MODEL-ADAPTER / 3. ModelResponse
MODEL-ADAPTER / 7. 错误映射

所有 Contract 类不可被 Provider 原地修改。
Secret 不进入 ModelRequest、repr、普通日志或 Trace。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any

# ── 角色与内容 ──


class MessageRole(StrEnum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class FinishReason(StrEnum):
    """ModelResponse 的标准终止原因。

    MODEL-ADAPTER / 3. ModelResponse：
    - stop: 模型正常结束
    - tool_calls: 模型请求调用 Tool
    - length: 达到 max_output_tokens
    - content_filter: 内容被过滤
    - cancelled: 被调用方取消
    - error: Provider 内部错误
    """
    stop = "stop"
    tool_calls = "tool_calls"
    length = "length"
    content_filter = "content_filter"
    cancelled = "cancelled"
    error = "error"


class ContentPartType(StrEnum):
    text = "text"
    image_url = "image_url"
    tool_use = "tool_use"
    tool_result = "tool_result"


# ── Usage ──


@dataclass(frozen=True)
class Usage:
    """模型调用 Token 用量。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


# ── ContentPart ──


@dataclass(frozen=True)
class ContentPart:
    """不可变内容片段。"""
    part_type: ContentPartType = ContentPartType.text
    text: str = ""
    image_url: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result_id: str | None = None
    trust_label: str = "unverified"

    def __repr__(self) -> str:
        if self.part_type == ContentPartType.text:
            return f"ContentPart(text={self.text[:50]!r}...)"
        return f"ContentPart({self.part_type})"


# ── ModelRequest ──


@dataclass(frozen=True)
class ModelRequest:
    """向 Provider 发送的统一请求。

    不包含 API Key 等 Secret。
    不支持的 ContentPartType 应被调用方拒绝而非静默忽略。
    """
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    model_role: str = "main"
    messages: tuple[dict[str, Any], ...] = ()
    tools: tuple[dict[str, Any], ...] = ()
    response_schema: dict[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    stop: tuple[str, ...] | None = None
    stream: bool = False
    timeout: timedelta | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)
    trace_context: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 冻结不可变
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        if self.stop is not None:
            object.__setattr__(self, "stop", tuple(self.stop))

    def __repr__(self) -> str:
        return (
            f"ModelRequest(id={self.request_id}, role={self.model_role}, "
            f"messages={len(self.messages)}, tools={len(self.tools)}, "
            f"stream={self.stream})"
        )


# ── ModelResponse ──


@dataclass(frozen=True)
class ModelResponse:
    """Provider 返回的标准响应。

    raw_response_ref 用于存储受限的原始响应引用。
    """
    request_id: str = ""
    provider_request_id: str = ""
    model_id: str = ""
    content_parts: tuple[ContentPart, ...] = ()
    tool_calls: tuple[dict[str, Any], ...] = ()
    structured_output: dict[str, Any] | None = None
    finish_reason: FinishReason = FinishReason.stop
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    raw_response_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_parts", tuple(self.content_parts))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))

    @property
    def text(self) -> str:
        """提取所有文本内容片段的拼接。"""
        return "".join(
            p.text for p in self.content_parts
            if p.part_type == ContentPartType.text
        )

    def __repr__(self) -> str:
        return (
            f"ModelResponse(id={self.request_id}, model={self.model_id}, "
            f"finish={self.finish_reason}, usage={self.usage})"
        )


# ── FinishReason 规范化 ──


FINISH_REASON_MAP: dict[str, FinishReason] = {
    "stop": FinishReason.stop,
    "end_turn": FinishReason.stop,
    "completed": FinishReason.stop,
    "tool_calls": FinishReason.tool_calls,
    "tool_use": FinishReason.tool_calls,
    "length": FinishReason.length,
    "max_tokens": FinishReason.length,
    "content_filter": FinishReason.content_filter,
    "cancelled": FinishReason.cancelled,
    "cancel": FinishReason.cancelled,
    "error": FinishReason.error,
    "error_limit": FinishReason.error,
}


def normalize_finish_reason(raw: str) -> FinishReason:
    """将 Provider 原始的 finish_reason 字符串标准化。"""
    return FINISH_REASON_MAP.get(raw.lower(), FinishReason.error)


# ── Provider 能力声明 ──


@dataclass(frozen=True)
class ModelCapabilities:
    """Provider 能力声明。"""
    context_window: int = 0
    max_output_tokens: int = 0
    modalities: tuple[str, ...] = ("text",)
    supports_streaming: bool = False
    supports_tools: bool = False
    supports_parallel_tools: bool = False
    supports_json_schema: bool = False
    supports_prompt_cache: bool = False
    tool_schema_limits: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "modalities", tuple(self.modalities))


# ── 标准 Provider 错误 ──


class ErrorCategory(StrEnum):
    """标准 Provider 错误分类。

    MODEL-ADAPTER / 7. 错误映射
    每个错误具有 retryable、retry_after 和安全消息。
    """
    authentication = "authentication"
    permission = "permission"
    model_not_found = "model_not_found"
    rate_limit = "rate_limit"
    context_overflow = "context_overflow"
    timeout = "timeout"
    connection = "connection"
    content_filter = "content_filter"
    invalid_request = "invalid_request"
    provider_internal = "provider_internal"
    cancelled = "cancelled"


@dataclass(frozen=True)
class ErrorEnvelope:
    """标准 Provider 错误信封。

    所有用户可见消息必须安全（不泄漏 Secret 或内部路径）。
    """
    category: ErrorCategory = ErrorCategory.provider_internal
    message: str = "An unknown error occurred"
    retryable: bool = False
    retry_after: timedelta | None = None
    original_type: str = ""
    original_message: str = ""

    def __repr__(self) -> str:
        return f"ErrorEnvelope({self.category}, retryable={self.retryable})"


# ── 默认错误分类规则 ──


def classify_error(error_type: str, status_code: int = 0) -> ErrorEnvelope:
    """根据 Provider 返回的错误类型和状态码分类。"""
    error_type_lower = error_type.lower()

    # Authentication
    if any(kw in error_type_lower for kw in ("auth", "unauthorized", "403", "401")):
        return ErrorEnvelope(
            category=ErrorCategory.authentication,
            message="Authentication failed",
            retryable=False,
            original_type=error_type,
        )

    # Rate limit
    if any(kw in error_type_lower for kw in ("rate", "429", "too_many")):
        return ErrorEnvelope(
            category=ErrorCategory.rate_limit,
            message="Rate limit exceeded",
            retryable=True,
            retry_after=timedelta(seconds=30),
            original_type=error_type,
        )

    # Context overflow
    if any(kw in error_type_lower for kw in ("context", "length", "token", "max_tokens")):
        return ErrorEnvelope(
            category=ErrorCategory.context_overflow,
            message="Context window exceeded",
            retryable=False,
            original_type=error_type,
        )

    # Timeout
    if any(kw in error_type_lower for kw in ("timeout", "deadline")):
        return ErrorEnvelope(
            category=ErrorCategory.timeout,
            message="Request timed out",
            retryable=True,
            retry_after=timedelta(seconds=10),
            original_type=error_type,
        )

    # Connection
    if any(kw in error_type_lower for kw in ("connection", "network", "reset", "econn")):
        return ErrorEnvelope(
            category=ErrorCategory.connection,
            message="Connection error",
            retryable=True,
            retry_after=timedelta(seconds=5),
            original_type=error_type,
        )

    # Content filter
    if any(kw in error_type_lower for kw in ("content_filter", "safety", "moderation")):
        return ErrorEnvelope(
            category=ErrorCategory.content_filter,
            message="Content filtered by safety policy",
            retryable=False,
            original_type=error_type,
        )

    # Model not found
    if any(kw in error_type_lower for kw in ("model_not_found", "not_found", "404")):
        return ErrorEnvelope(
            category=ErrorCategory.model_not_found,
            message="Model not found or unavailable",
            retryable=False,
            original_type=error_type,
        )

    # Provider internal
    if any(kw in error_type_lower for kw in ("internal", "server", "500", "502", "503")):
        return ErrorEnvelope(
            category=ErrorCategory.provider_internal,
            message="Provider internal error",
            retryable=True,
            retry_after=timedelta(seconds=5),
            original_type=error_type,
        )

    return ErrorEnvelope(
        category=ErrorCategory.provider_internal,
        message="An unknown provider error occurred",
        retryable=True,
        retry_after=timedelta(seconds=10),
        original_type=error_type,
    )
