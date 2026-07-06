"""Cross-module contracts — Envelopes, Requests, Replies, Errors."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from cogito.contracts.trace_context import TraceContext


class ReplyMode(StrEnum):
    """Agent 回复模式。"""
    normal = "normal"
    placeholder = "placeholder"
    streaming = "streaming"
    edit = "edit"
    silent = "silent"


class ToolStatus(StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"
    rejected = "rejected"
    cancelled = "cancelled"


class ErrorCategory(StrEnum):
    validation = "validation"
    policy_denied = "policy_denied"
    authentication = "authentication"
    authorization = "authorization"
    rate_limit = "rate_limit"
    timeout = "timeout"
    dependency_unavailable = "dependency_unavailable"
    conflict = "conflict"
    not_found = "not_found"
    resource_exhausted = "resource_exhausted"
    side_effect_unknown = "side_effect_unknown"
    internal = "internal"


# ─── Protected fields that cannot be modified by middleware ───

PROTECTED_FIELDS = frozenset({
    "trace_id", "principal_id", "conversation_id", "turn_id",
    "attempt_id", "origin", "reply_route", "schema_version",
    "idempotency_key",
})


# ─── ReplyRoute ───


@dataclass
class ReplyRoute:
    """回复路由快照。"""
    channel_instance_id: str = ""
    platform_conversation_id: str = ""
    thread_id: str | None = None
    reply_to_platform_message_id: str | None = None
    reply_token: str = ""
    reply_token_expires_at: datetime | None = None
    target_endpoint_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_instance_id": self.channel_instance_id,
            "platform_conversation_id": self.platform_conversation_id,
            "thread_id": self.thread_id,
            "reply_to_platform_message_id": self.reply_to_platform_message_id,
            "reply_token": self.reply_token,
            "reply_token_expires_at": self.reply_token_expires_at.isoformat() if self.reply_token_expires_at else None,
            "target_endpoint_ref": self.target_endpoint_ref,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplyRoute:
        return cls(
            channel_instance_id=data.get("channel_instance_id", ""),
            platform_conversation_id=data.get("platform_conversation_id", ""),
            thread_id=data.get("thread_id"),
            reply_to_platform_message_id=data.get("reply_to_platform_message_id"),
            reply_token=data.get("reply_token", ""),
            reply_token_expires_at=datetime.fromisoformat(data["reply_token_expires_at"]) if data.get("reply_token_expires_at") else None,
            target_endpoint_ref=data.get("target_endpoint_ref"),
        )


# ─── ErrorEnvelope ───


@dataclass
class ErrorEnvelope:
    """标准错误响应。"""
    error_code: str = "internal_error"
    category: ErrorCategory = ErrorCategory.internal
    message: str = ""
    retryable: bool = False
    retry_after: float | None = None
    source_component: str = ""
    safe_details: str = ""
    internal_payload_ref: str | None = None
    caused_by: str | None = None
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "category": self.category.value,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "source_component": self.source_component,
            "safe_details": self.safe_details,
            "internal_payload_ref": self.internal_payload_ref,
            "caused_by": self.caused_by,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ErrorEnvelope:
        return cls(
            error_code=data.get("error_code", "internal_error"),
            category=ErrorCategory(data.get("category", "internal")),
            message=data.get("message", ""),
            retryable=data.get("retryable", False),
            retry_after=data.get("retry_after"),
            source_component=data.get("source_component", ""),
            safe_details=data.get("safe_details", ""),
            internal_payload_ref=data.get("internal_payload_ref"),
            caused_by=data.get("caused_by"),
            trace_id=data.get("trace_id", ""),
        )


# ─── ChannelEnvelope ───


@dataclass
class ChannelEnvelope:
    """Gateway 入站消息信封。"""
    schema_version: str = "1.0"
    message_id: str = ""
    channel_type: str = ""
    channel_instance_id: str = ""
    platform_sender_id: str = ""
    sender_endpoint_ref: str = ""
    conversation_endpoint_ref: str = ""
    platform_conversation_id: str = ""
    thread_id: str | None = None
    content_parts: list[dict[str, Any]] = field(default_factory=list)
    platform_message_id: str = ""
    reply_route: ReplyRoute | None = None
    received_at: str = ""
    trust_label: str = "unverified"
    capability_snapshot: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] | None = None
    trace_context: TraceContext | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message_id:
            self.message_id = uuid.uuid4().hex
        if not self.received_at:
            self.received_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "channel_type": self.channel_type,
            "channel_instance_id": self.channel_instance_id,
            "platform_sender_id": self.platform_sender_id,
            "sender_endpoint_ref": self.sender_endpoint_ref,
            "conversation_endpoint_ref": self.conversation_endpoint_ref,
            "platform_conversation_id": self.platform_conversation_id,
            "thread_id": self.thread_id,
            "content_parts": self.content_parts,
            "platform_message_id": self.platform_message_id,
            "reply_route": self.reply_route.to_dict() if self.reply_route else None,
            "received_at": self.received_at,
            "trust_label": self.trust_label,
            "capability_snapshot": self.capability_snapshot,
            "raw_payload": self.raw_payload,
            "trace_context": self.trace_context.to_dict() if self.trace_context else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelEnvelope:
        return cls(
            schema_version=data.get("schema_version", "1.0"),
            message_id=data.get("message_id", ""),
            channel_type=data.get("channel_type", ""),
            channel_instance_id=data.get("channel_instance_id", ""),
            platform_sender_id=data.get("platform_sender_id", ""),
            sender_endpoint_ref=data.get("sender_endpoint_ref", ""),
            conversation_endpoint_ref=data.get("conversation_endpoint_ref", ""),
            platform_conversation_id=data.get("platform_conversation_id", ""),
            thread_id=data.get("thread_id"),
            content_parts=data.get("content_parts", []),
            platform_message_id=data.get("platform_message_id", ""),
            reply_route=ReplyRoute.from_dict(data["reply_route"]) if data.get("reply_route") else None,
            received_at=data.get("received_at", ""),
            trust_label=data.get("trust_label", "unverified"),
            capability_snapshot=data.get("capability_snapshot", {}),
            raw_payload=data.get("raw_payload"),
            trace_context=TraceContext.from_dict(data["trace_context"]) if data.get("trace_context") else None,
            metadata=data.get("metadata", {}),
        )


# ─── AgentRequest / AgentReply ───


@dataclass
class AgentRequest:
    """Agent 运行时输入。"""
    turn_id: str = ""
    principal: dict[str, Any] = field(default_factory=dict)
    conversation: dict[str, Any] = field(default_factory=dict)
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    input_message: dict[str, Any] = field(default_factory=dict)
    capability_policy: dict[str, Any] = field(default_factory=dict)
    resource_budget: dict[str, Any] = field(default_factory=dict)
    trace_context: TraceContext | None = None


@dataclass
class AgentReply:
    """Agent 运行时输出。"""
    turn_id: str = ""
    content_parts: list[dict[str, Any]] = field(default_factory=list)
    reply_mode: ReplyMode = ReplyMode.normal
    render_hints: dict[str, Any] = field(default_factory=dict)
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)
    suggested_tasks: list[dict[str, Any]] = field(default_factory=list)
    status_summary: str = ""
    trace_context: TraceContext | None = None


# ─── ToolRequest / ToolResult ───


@dataclass
class ToolRequest:
    """Tool 执行请求。"""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_version: str = "1.0"
    arguments: dict[str, Any] = field(default_factory=dict)
    requested_permissions: list[str] = field(default_factory=list)
    idempotency_key: str = ""
    timeout: float = 30.0
    risk_context: str = "none"
    trace_context: TraceContext | None = None

    def __post_init__(self) -> None:
        if not self.tool_call_id:
            self.tool_call_id = uuid.uuid4().hex


@dataclass
class ToolResult:
    """Tool 执行结果。"""
    tool_call_id: str = ""
    status: ToolStatus = ToolStatus.succeeded
    structured_output: dict[str, Any] = field(default_factory=dict)
    output_ref: str | None = None
    error: str | None = None
    side_effect_receipt: dict[str, Any] | None = None
    started_at: str = ""
    completed_at: str = ""


# ─── CommandEnvelope ───


@dataclass
class CommandEnvelope:
    """命令契约信封。"""
    command_id: str = ""
    command_type: str = ""
    aggregate_type: str = ""
    aggregate_id: str = ""
    expected_version: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    principal_id: str = ""
    trace_context: TraceContext | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.command_id:
            self.command_id = uuid.uuid4().hex


# ─── EventEnvelope ───


@dataclass
class EventEnvelope:
    """事件信封。"""
    schema_version: str = "1.0"
    event_id: str = ""
    event_type: str = ""
    source: str = ""
    aggregate_type: str = ""
    aggregate_id: str = ""
    aggregate_version: int = 1
    occurred_at: str = ""
    ingested_at: str | None = None
    payload_ref: str | None = None
    content_hash: str = ""
    trust_label: str = "unverified"
    origin: str = "system"
    correlation_id: str = ""
    causation_id: str = ""
    trace_context: TraceContext | None = None

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = uuid.uuid4().hex
        if not self.occurred_at:
            self.occurred_at = datetime.now(timezone.utc).isoformat()
