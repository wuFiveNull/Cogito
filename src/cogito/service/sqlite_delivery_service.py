"""Event-first DeliveryService Protocol implementation.

提供 enqueue / get / cancel / retry / reconcile 作为 Delivery 聚合的唯一
写入口。所有状态由 Delivery Event 流重放，待执行的外部副作用由
``CanonicalEffectWorker`` 从受保护 payload 执行。

GatewayClient 抽象：service 层通过 GatewayClient Protocol 访问平台适配器；
LoopbackGatewayClient（合并进程）复用现有 ChannelManager + Adapter。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from cogito.contracts.clock import Clock, epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.delivery_effect_payload import (
    DeliveryEffectPayload,
    load_delivery_effect_payload,
    store_delivery_effect_payload,
)
from cogito.service.delivery_service import (
    DeliveryRef,
    DeliveryRequest,
    DeliveryService,
    DeliveryView,
    ReconcileResult,
)
from cogito.service.gateway_client import GatewayClient, GatewayResult
from cogito.store.event_replay import replay_delivery
from cogito.store.event_store import EventStore


def _now_ms(clock: Clock | None = None) -> int:
    c = clock
    if c is None:
        from cogito.contracts.clock import ProductionClock

        c = ProductionClock()
    return epoch_ms(c.now())


# ── SqliteDeliveryService ──────────────────────────────────────────────


class SqliteDeliveryService(DeliveryService):  # type: ignore[override]
    """DeliveryService Protocol 的唯一 SQLite 写实现。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        gateway: Any | None = None,
        *,
        effect_payload_store: PayloadStore,
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._gateway = gateway or _UnavailableGateway()
        self._effect_payload_store = effect_payload_store

    async def enqueue(self, request: DeliveryRequest) -> DeliveryRef:
        delivery_id = f"del-{uuid.uuid4().hex[:16]}"
        now = _now_ms(self._clock)
        idem = request.idempotency_key or f"auto-{delivery_id}"
        event_store = EventStore(self._conn)
        command_key = f"delivery-request:{idem}"

        existing_event = event_store.find_idempotent("sqlite-delivery-service", command_key)
        if existing_event is not None:
            return DeliveryRef(delivery_id=existing_event.stream_id)

        initial_status = "scheduled" if request.scheduled_at else "pending"
        # ChannelGateway uses these fields to construct a stable platform
        # message ID and idempotency key.  Proactive callers provide a small
        # target dict, so add the Delivery identity at the boundary.
        target_snapshot = dict(request.target)
        target_snapshot.setdefault("delivery_id", delivery_id)
        target_snapshot.setdefault("idempotency_key", idem)
        payload_ref, payload_hash = store_delivery_effect_payload(
            self._effect_payload_store,
            DeliveryEffectPayload(
                delivery_id=delivery_id,
                target_snapshot=target_snapshot,
                content=self._resolve_effect_content(request.content_ref),
                content_ref=request.content_ref,
                idempotency_key=idem,
                scheduled_at=request.scheduled_at,
            ),
        )
        event_store.append(
            Event(
                event_type="delivery.requested",
                stream_type="delivery",
                stream_id=delivery_id,
                producer="sqlite-delivery-service",
                event_class=EventClass.DOMAIN,
                context=EventContext(),
                summary="Delivery requested",
                attributes={
                    "effect_payload_kind": "delivery-effect.v2",
                    **({"scheduled_at": request.scheduled_at} if request.scheduled_at else {}),
                },
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                outcome=initial_status,
                occurred_at=now,
                idempotency_key=command_key,
            )
        )
        self._conn.commit()
        return DeliveryRef(delivery_id=delivery_id)

    def get(self, delivery_id: str) -> DeliveryView | None:
        events = EventStore(self._conn).read_stream("delivery", delivery_id)
        projection = replay_delivery(events, delivery_id)
        if projection is None:
            return None
        return self._event_view(events, projection.status, projection.stream_version)

    async def cancel(self, delivery_id: str, expected_version: int) -> None:
        event_store = EventStore(self._conn)
        events = event_store.read_stream("delivery", delivery_id)
        projection = replay_delivery(events, delivery_id)
        if projection is None or projection.status not in {
            "pending",
            "scheduled",
            "retry_scheduled",
        }:
            return
        event_store.append(
            Event(
                event_type="delivery.cancelled",
                stream_type="delivery",
                stream_id=delivery_id,
                producer="sqlite-delivery-service",
                event_class=EventClass.DOMAIN,
                summary="Delivery cancelled",
                outcome="cancelled",
                idempotency_key=f"delivery:{delivery_id}:cancel:{projection.stream_version}",
            ),
            expected_version=expected_version if expected_version > 0 else None,
        )
        self._conn.commit()

    async def retry(self, delivery_id: str, expected_version: int) -> None:
        event_store = EventStore(self._conn)
        events = event_store.read_stream("delivery", delivery_id)
        projection = replay_delivery(events, delivery_id)
        if projection is None or projection.status != "retry_scheduled":
            return
        request = next(event for event in events if event.event_type == "delivery.requested")
        event_store.append(
            Event(
                event_type="delivery.retry_requested",
                stream_type="delivery",
                stream_id=delivery_id,
                producer="sqlite-delivery-service",
                event_class=EventClass.DOMAIN,
                context=EventContext(
                    trace_id=request.context.trace_id,
                    causation_id=request.event_id,
                ),
                summary="Delivery retry requested",
                outcome="pending",
                idempotency_key=f"delivery:{delivery_id}:retry:{projection.stream_version}",
            ),
            expected_version=expected_version if expected_version > 0 else None,
        )
        self._conn.commit()

    async def reconcile(
        self,
        delivery_id: str,
        platform_message_id: str | None = None,
        *,
        confirmed: bool = False,
    ) -> ReconcileResult:
        event_store = EventStore(self._conn)
        events = event_store.read_stream("delivery", delivery_id)
        projection = replay_delivery(events, delivery_id)
        if projection is None:
            return ReconcileResult(delivery_id=delivery_id, status="still_unknown")
        if projection.status == "completed":
            return ReconcileResult(
                delivery_id=delivery_id,
                status="sent",
                platform_message_id=projection.platform_message_id or platform_message_id,
            )
        if projection.status != "unknown":
            return ReconcileResult(delivery_id=delivery_id, status="still_unknown")
        platform_message_id = platform_message_id or projection.platform_message_id
        if confirmed:
            return self._confirm_event_delivery(events, platform_message_id)
        result = self._reconcile_event_delivery(events, platform_message_id)
        if result is not None:
            return result
        return ReconcileResult(delivery_id=delivery_id, status="still_unknown")

    def _confirm_event_delivery(
        self,
        events: list[Event],
        platform_message_id: str | None,
    ) -> ReconcileResult:
        request = next(event for event in events if event.event_type == "delivery.requested")
        EventStore(self._conn).append(
            Event(
                event_type="delivery.completed",
                stream_type="delivery",
                stream_id=request.stream_id,
                producer="sqlite-delivery-reconcile",
                event_class=EventClass.DOMAIN,
                context=EventContext(
                    trace_id=request.context.trace_id,
                    causation_id=request.event_id,
                ),
                summary="Delivery manually reconciled",
                attributes=(
                    {"platform_message_id": platform_message_id}
                    if platform_message_id
                    else {}
                ),
                outcome="completed",
                idempotency_key=f"delivery:{request.stream_id}:manual-reconciled",
            ),
            expected_version=len(events),
        )
        self._conn.commit()
        return ReconcileResult(request.stream_id, "sent", platform_message_id)

    def _event_view(
        self,
        events: list[Event],
        status: str,
        stream_version: int,
    ) -> DeliveryView:
        request = next(event for event in events if event.event_type == "delivery.requested")
        target: dict[str, Any] = {}
        content_ref: str | None = None
        idempotency_key = ""
        if request.payload_ref:
            try:
                payload = load_delivery_effect_payload(
                    self._effect_payload_store,
                    request.payload_ref,
                )
                target = payload.target_snapshot
                content_ref = payload.content_ref
                idempotency_key = payload.idempotency_key
            except (LookupError, ValueError):
                pass
        attempts = [
            {"event_id": event.event_id, "started_at": event.occurred_at}
            for event in events
            if event.event_type == "delivery.started"
        ]
        receipts = [
            {
                "event_id": event.event_id,
                "receipt_kind": event.event_type.rsplit(".", 1)[-1],
                "platform_message_id": event.attributes.get("platform_message_id"),
                "observed_at": event.occurred_at,
            }
            for event in events
            if event.event_type in {"delivery.completed", "delivery.unknown"}
        ]
        platform_message_id = next(
            (
                str(event.attributes["platform_message_id"])
                for event in reversed(events)
                if event.attributes.get("platform_message_id")
            ),
            None,
        )
        return DeliveryView(
            delivery_id=request.stream_id,
            status=status,
            target_snapshot=target,
            content_ref=content_ref,
            idempotency_key=idempotency_key,
            attempt_count=len(attempts),
            platform_message_id=platform_message_id,
            attempts=attempts,
            receipts=receipts,
            stream_version=stream_version,
        )

    def _reconcile_event_delivery(
        self,
        events: list[Event],
        platform_message_id: str | None,
    ) -> ReconcileResult | None:
        request = next(event for event in events if event.event_type == "delivery.requested")
        if not request.payload_ref:
            return None
        try:
            payload = load_delivery_effect_payload(self._effect_payload_store, request.payload_ref)
        except (LookupError, ValueError):
            return None
        target = json.dumps(
            payload.target_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            result = self._gateway.reconcile(target, platform_message_id, payload.idempotency_key)
        except Exception:
            result = GatewayResult(status="unknown", error_code="gateway_exception")
        if result.status in {"success", "sent"}:
            platform_message_id = result.platform_message_id or platform_message_id
            EventStore(self._conn).append(
                Event(
                    event_type="delivery.completed",
                    stream_type="delivery",
                    stream_id=request.stream_id,
                    producer="sqlite-delivery-reconcile",
                    event_class=EventClass.DOMAIN,
                    context=EventContext(
                        trace_id=request.context.trace_id,
                        causation_id=request.event_id,
                    ),
                    summary="Delivery reconciled",
                    attributes=(
                        {"platform_message_id": platform_message_id}
                        if platform_message_id
                        else {}
                    ),
                    outcome="completed",
                    idempotency_key=f"delivery:{request.stream_id}:reconciled",
                ),
                expected_version=len(events),
            )
            self._conn.commit()
            return ReconcileResult(request.stream_id, "sent", platform_message_id)
        if result.status in {
            "permanent",
            "auth_error",
            "route_expired",
            "unsupported",
            "too_large",
        }:
            EventStore(self._conn).append(
                Event(
                    event_type="delivery.failed",
                    stream_type="delivery",
                    stream_id=request.stream_id,
                    producer="sqlite-delivery-reconcile",
                    event_class=EventClass.DOMAIN,
                    context=EventContext(
                        trace_id=request.context.trace_id,
                        causation_id=request.event_id,
                    ),
                    summary="Delivery reconciliation failed",
                    outcome="failed",
                    error_category=result.error_code or result.status,
                    idempotency_key=f"delivery:{request.stream_id}:reconcile-failed",
                ),
                expected_version=len(events),
            )
            self._conn.commit()
            return ReconcileResult(request.stream_id, "failed", result.platform_message_id)
        return None

    def _resolve_effect_content(self, content_ref: str) -> str:
        """Capture the resolved body before appending an effect request.

        ``content_parts`` is a transitional compatibility read for message-id
        callers.  The canonical effect executor receives the resulting body
        from the protected Event payload and never reads this table.
        """
        if not content_ref:
            return ""
        # Try PayloadStore-based reading first
        if self._effect_payload_store is not None:
            try:
                from cogito.store.event_message_reader import EventMessageReader
                from cogito.store.event_store import EventStore

                reader = EventMessageReader(self._conn, self._effect_payload_store)
                msg = reader.get(content_ref)
                if msg is not None:
                    for part in msg.content_parts:
                        if part.content_type in ("text", "markdown") and part.inline_data:
                            return part.inline_data
            except Exception:
                pass
        row = self._conn.execute(
            "SELECT inline_data FROM content_parts "
            "WHERE message_id=? AND content_type='text' ORDER BY rowid LIMIT 1",
            (content_ref,),
        ).fetchone()
        return str(row[0]) if row is not None else content_ref


class _UnavailableGateway:
    """Safe default used by enqueue-only maintenance and migration paths."""

    def send(self, target_snapshot: str, content: str, idempotency_key: str) -> GatewayResult:
        return GatewayResult(status="unknown", error_code="gateway_not_configured")


# Compatibility re-exports for callers that imported the Port from this module.
__all__ = [
    "GatewayClient",
    "GatewayResult",
    "SqliteDeliveryService",
]
