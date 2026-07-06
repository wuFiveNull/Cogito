"""
cogito.bus.events_lifecycle — 不可变生命周期事件

所有事件均为 frozen dataclass，遵循以下规则：
1. Event 不允许被 Handler 修改；
2. Event Handler 不参与核心事务；
3. Event Handler 失败不能破坏 Turn 的已提交状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from cogito.bus.events import OutboundRequest


def _now() -> datetime:
    return datetime.now()


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


# ── 基础事件 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LifecycleEvent:
    """所有生命周期事件的基类，包含通用追踪字段。"""
    event_id: str = field(default_factory=_new_id)
    event_type: str = ""
    occurred_at: datetime = field(default_factory=_now)

    trace_id: str = ""
    session_key: str | None = None
    turn_id: str | None = None
    message_id: str | None = None
    outbound_id: str | None = None

    metadata: Mapping[str, Any] = field(default_factory=dict)


# ── 入站生命周期 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class InboundReceived(LifecycleEvent):
    event_type: str = "inbound_received"


@dataclass(frozen=True)
class InboundAccepted(LifecycleEvent):
    event_type: str = "inbound_accepted"


@dataclass(frozen=True)
class InboundDuplicateIgnored(LifecycleEvent):
    event_type: str = "inbound_duplicate_ignored"
    existing_message_id: str = ""


# ── Turn 生命周期 ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnQueued(LifecycleEvent):
    event_type: str = "turn_queued"


@dataclass(frozen=True)
class TurnStarted(LifecycleEvent):
    event_type: str = "turn_started"

    @classmethod
    def from_context(cls, context, **overrides):
        """从 TurnContext 构建 TurnStarted 事件。"""
        return cls(
            trace_id=context.trace_id,
            session_key=context.session_key,
            turn_id=context.turn_id,
            message_id=context.trigger_message_id,
            **overrides,
        )


@dataclass(frozen=True)
class TurnCancelRequested(LifecycleEvent):
    event_type: str = "turn_cancel_requested"


@dataclass(frozen=True)
class TurnCancelled(LifecycleEvent):
    event_type: str = "turn_cancelled"


@dataclass(frozen=True)
class TurnFailed(LifecycleEvent):
    event_type: str = "turn_failed"
    error: str = ""


# ── LLM 调用生命周期 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMCallStarted(LifecycleEvent):
    event_type: str = "llm_call_started"
    model: str = ""


@dataclass(frozen=True)
class LLMCallCompleted(LifecycleEvent):
    event_type: str = "llm_call_completed"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMCallFailed(LifecycleEvent):
    event_type: str = "llm_call_failed"
    model: str = ""
    error: str = ""


# ── 工具调用生命周期 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCallStarted(LifecycleEvent):
    event_type: str = "tool_call_started"
    tool_name: str = ""


@dataclass(frozen=True)
class ToolCallCompleted(LifecycleEvent):
    event_type: str = "tool_call_completed"
    tool_name: str = ""
    duration_ms: float = 0.0


@dataclass(frozen=True)
class ToolCallFailed(LifecycleEvent):
    event_type: str = "tool_call_failed"
    tool_name: str = ""
    error: str = ""


# ── 提交生命周期 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnCommitting(LifecycleEvent):
    event_type: str = "turn_committing"


@dataclass(frozen=True)
class TurnCommitted(LifecycleEvent):
    event_type: str = "turn_committed"

    @classmethod
    def from_context(cls, context, **overrides):
        """从 TurnContext 构建 TurnCommitted 事件。"""
        return cls(
            trace_id=context.trace_id,
            session_key=context.session_key,
            turn_id=context.turn_id,
            message_id=context.trigger_message_id,
            **overrides,
        )


# ── 出站生命周期 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutboundAccepted(LifecycleEvent):
    event_type: str = "outbound_accepted"


@dataclass(frozen=True)
class DeliveryStarted(LifecycleEvent):
    event_type: str = "delivery_started"


@dataclass(frozen=True)
class DeliverySucceeded(LifecycleEvent):
    event_type: str = "delivery_succeeded"
    external_message_id: str = ""


@dataclass(frozen=True)
class DeliveryRetryScheduled(LifecycleEvent):
    event_type: str = "delivery_retry_scheduled"
    attempt: int = 0
    next_attempt_at: datetime | None = None


@dataclass(frozen=True)
class DeliveryFailed(LifecycleEvent):
    event_type: str = "delivery_failed"
    attempt: int = 0
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class DeliveryDead(LifecycleEvent):
    event_type: str = "delivery_dead"
    error_code: str = ""
    error_message: str = ""
