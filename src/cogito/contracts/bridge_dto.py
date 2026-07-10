"""LangBot Bridge 版本化 DTO（Plan 05 M2）。

定义 Gateway ↔ Agent Core 之间的入站/出站契约。
- InboundMessage: 入站消息（Gateway → Core）
- DeliveryOperation: 出站投递操作（Core → Gateway）
- 双版本并存：V0（旧）和 V1（当前），破坏性变化只增加新版本
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ── 错误码 ──

BRIDGE_ERROR_CODES = frozenset({
    "route_expired",
    "rate_limit",
    "auth_failed",
    "unsupported",
    "too_large",
    "temporary",
    "permanent",
})


# ── 公共值对象 ──

@dataclass(frozen=True)
class ContentPart:
    type: str  # text / image / voice / file / at
    data: str = ""
    mime: str | None = None
    name: str | None = None
    size: int = 0


@dataclass(frozen=True)
class ReplyRoute:
    """回复路由快照 —— Token 过期返回 route_expired。"""
    adapter_id: str
    channel_type: str
    conversation_id: str
    endpoint_ref: str
    token: str = ""
    expires_at: str | None = None  # RFC3339


@dataclass(frozen=True)
class TargetSnapshot:
    """主动发送时固定的投递目标。"""
    adapter_id: str
    channel_type: str
    conversation_id: str
    endpoint_ref: str
    platform: str = ""


@dataclass(frozen=True)
class TraceContext:
    trace_id: str = ""
    span_id: str = ""
    origin: str = ""


# ── V1 DTO（当前版本）──

def _parts_to_list(parts: list[ContentPart]) -> list[dict]:
    return [
        {
            "type": p.type,
            "data": p.data,
            "mime": p.mime,
            "name": p.name,
            "size": p.size,
        }
        for p in parts
    ]


def _parts_from_list(items: list[dict]) -> list[ContentPart]:
    return [
        ContentPart(
            type=p.get("type", "text"),
            data=p.get("data", ""),
            mime=p.get("mime"),
            name=p.get("name"),
            size=int(p.get("size") or 0),
        )
        for p in items
    ]


@dataclass(frozen=True)
class InboundMessage:
    """入站消息（Gateway → Core）。"""
    schema_version: str = "1"
    event_id: str = ""
    channel_name: str = ""
    instance_id: str = ""
    conversation_ref: str = ""        # 不透明稳定字符串
    thread_ref: str | None = None
    sender_ref: str = ""              # 不透明，Core 不解析内部
    content_parts: list[ContentPart] = field(default_factory=list)
    reply_route: ReplyRoute | None = None
    trust_label: str = "external_untrusted"
    capability_ref: str = ""
    raw_payload_ref: str = ""
    trace: TraceContext = field(default_factory=TraceContext)
    received_at: str = ""             # RFC3339

    def to_json(self) -> str:
        return json.dumps({
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "channel_name": self.channel_name,
            "instance_id": self.instance_id,
            "conversation_ref": self.conversation_ref,
            "thread_ref": self.thread_ref,
            "sender_ref": self.sender_ref,
            "content_parts": _parts_to_list(self.content_parts),
            "reply_route": self.reply_route.__dict__ if self.reply_route else None,
            "trust_label": self.trust_label,
            "capability_ref": self.capability_ref,
            "raw_payload_ref": self.raw_payload_ref,
            "trace": self.trace.__dict__,
            "received_at": self.received_at,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str | dict) -> InboundMessage:
        """解码（支持 V1 直接解析）。"""
        obj = json.loads(data) if isinstance(data, str) else data
        return cls._from_dict(obj)

    @classmethod
    def _from_dict(cls, obj: dict) -> InboundMessage:
        parts = _parts_from_list(obj.get("content_parts", []))
        route_obj = obj.get("reply_route")
        route = ReplyRoute(**route_obj) if route_obj else None
        trace_obj = obj.get("trace") or {}
        trace = TraceContext(
            trace_id=trace_obj.get("trace_id", ""),
            span_id=trace_obj.get("span_id", ""),
            origin=trace_obj.get("origin", ""),
        )
        return cls(
            schema_version=str(obj.get("schema_version", "1")),
            event_id=obj.get("event_id", ""),
            channel_name=obj.get("channel_name", ""),
            instance_id=obj.get("instance_id", ""),
            conversation_ref=obj.get("conversation_ref", ""),
            thread_ref=obj.get("thread_ref"),
            sender_ref=obj.get("sender_ref", ""),
            content_parts=parts,
            reply_route=route,
            trust_label=obj.get("trust_label", "external_untrusted"),
            capability_ref=obj.get("capability_ref", ""),
            raw_payload_ref=obj.get("raw_payload_ref", ""),
            trace=trace,
            received_at=obj.get("received_at", ""),
        )


@dataclass(frozen=True)
class DeliveryOperation:
    """出站投递操作（Core → Gateway）。"""
    schema_version: str = "1"
    operation_id: str = ""
    delivery_id: str = ""
    attempt_id: str = ""
    operation_seq: int = 1
    idempotency_key: str = ""
    target_snapshot: TargetSnapshot | None = None
    action: str = "send"
    # send / start_placeholder / append_or_replace / finish / delete / reconcile
    content: list[ContentPart] = field(default_factory=list)
    platform_message_id: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "delivery_id": self.delivery_id,
            "attempt_id": self.attempt_id,
            "operation_seq": self.operation_seq,
            "idempotency_key": self.idempotency_key,
            "target_snapshot": self.target_snapshot.__dict__ if self.target_snapshot else None,
            "action": self.action,
            "content": _parts_to_list(self.content),
            "platform_message_id": self.platform_message_id,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str | dict) -> DeliveryOperation:
        obj = json.loads(data) if isinstance(data, str) else data
        return cls._from_dict(obj)

    @classmethod
    def _from_dict(cls, obj: dict) -> DeliveryOperation:
        parts = _parts_from_list(obj.get("content", []))
        target_obj = obj.get("target_snapshot")
        target = TargetSnapshot(**target_obj) if target_obj else None
        return cls(
            schema_version=str(obj.get("schema_version", "1")),
            operation_id=obj.get("operation_id", ""),
            delivery_id=obj.get("delivery_id", ""),
            attempt_id=obj.get("attempt_id", ""),
            operation_seq=obj.get("operation_seq", 1),
            idempotency_key=obj.get("idempotency_key", ""),
            target_snapshot=target,
            action=obj.get("action", "send"),
            content=parts,
            platform_message_id=obj.get("platform_message_id"),
        )


# ── V0 → V1 升级器 ──

@dataclass(frozen=True)
class DeliveryOperationV0:
    """V0 出站投递（旧版，无 schema_version）。"""
    delivery_id: str = ""
    attempt_id: str = ""
    channel: str = ""
    conversation_id: str = ""
    text: str = ""
    operation_seq: int = 1

    def to_v1(self) -> DeliveryOperation:
        """升级到 V1。"""
        return DeliveryOperation(
            schema_version="1",
            operation_id=f"op-{uuid.uuid4().hex[:12]}",
            delivery_id=self.delivery_id,
            attempt_id=self.attempt_id,
            operation_seq=self.operation_seq,
            idempotency_key=f"{self.delivery_id}:{self.operation_seq}",
            target_snapshot=TargetSnapshot(
                adapter_id="",
                channel_type=self.channel,
                conversation_id=self.conversation_id,
                endpoint_ref=self.conversation_id,
            ),
            action="send",
            content=[ContentPart(type="text", data=self.text)] if self.text else [],
        )


@dataclass(frozen=True)
class InboundMessageV0:
    """V0 入站消息（旧版，无 schema_version）。"""
    event_id: str = ""
    channel: str = ""
    instance: str = ""
    conversation_id: str = ""
    sender_id: str = ""
    text: str = ""
    timestamp: int = 0

    def to_v1(self) -> InboundMessage:
        """升级到 V1。"""
        received = (
            datetime.fromtimestamp(self.timestamp, tz=UTC).isoformat()
            if self.timestamp
            else ""
        )
        return InboundMessage(
            schema_version="1",
            event_id=self.event_id,
            channel_name=self.channel,
            instance_id=self.instance,
            conversation_ref=self.conversation_id,
            sender_ref=self.sender_id,
            content_parts=[ContentPart(type="text", data=self.text)] if self.text else [],
            received_at=received,
        )


def decode_inbound(data: str | dict) -> InboundMessage:
    """自动检测版本并解码入站消息。"""
    obj = json.loads(data) if isinstance(data, str) else dict(data)
    ver = str(obj.get("schema_version", "0"))
    if ver == "1":
        return InboundMessage._from_dict(obj)
    # V0 或未知版本：走升级路径
    v0 = InboundMessageV0(
        event_id=obj.get("event_id", ""),
        channel=obj.get("channel", ""),
        instance=obj.get("instance", ""),
        conversation_id=obj.get("conversation_id", ""),
        sender_id=obj.get("sender_id", ""),
        text=obj.get("text", ""),
        timestamp=obj.get("timestamp", 0),
    )
    return v0.to_v1()


# ── 错误响应 ──

@dataclass(frozen=True)
class BridgeError:
    """Bridge 错误响应（不携带 Secret 原文）。"""
    error_code: str = ""
    message: str = ""
    retry_after_seconds: float | None = None

    def to_json(self) -> str:
        return json.dumps({
            "error_code": self.error_code,
            "message": self.message,
            "retry_after_seconds": self.retry_after_seconds,
        })
