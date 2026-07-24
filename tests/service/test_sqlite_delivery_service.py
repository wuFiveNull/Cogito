"""Event-first DeliveryService contract tests.

Delivery state is asserted through the canonical Event stream.  The legacy
``deliveries`` table must remain untouched by every public operation here.
"""

from __future__ import annotations

import sqlite3

import pytest

from cogito.contracts.clock import FakeClock
from cogito.domain.event import Event, EventClass
from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.canonical_delivery_effect_executor import CanonicalDeliveryEffectExecutor
from cogito.service.delivery_service import DeliveryRequest
from cogito.service.event_effect_worker import CanonicalEffectWorker
from cogito.service.gateway_client import GatewayResult
from cogito.service.sqlite_delivery_service import SqliteDeliveryService
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate


class FakeGatewayClient:
    """Gateway fake with deterministic canonical-effect outcomes."""

    def __init__(
        self,
        *,
        status: str = "success",
        platform_message_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self._status = status
        self._platform_message_id = platform_message_id
        self._error_code = error_code
        self.send_calls: list[tuple[str, str, str]] = []

    def send(self, target_snapshot: str, content: str, idempotency_key: str) -> GatewayResult:
        self.send_calls.append((target_snapshot, content, idempotency_key))
        return GatewayResult(
            status=self._status,
            platform_message_id=self._platform_message_id,
            error_code=self._error_code,
        )


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    migrate(connection)
    return connection


@pytest.fixture
def payload_store(conn: sqlite3.Connection, tmp_path) -> PayloadStore:
    return PayloadStore(tmp_path / "payloads", conn)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _service(
    conn: sqlite3.Connection,
    payload_store: PayloadStore,
    clock: FakeClock,
    gateway: FakeGatewayClient | None = None,
) -> SqliteDeliveryService:
    return SqliteDeliveryService(
        conn,
        gateway or FakeGatewayClient(),
        effect_payload_store=payload_store,
        clock=clock,
    )


def _append_delivery_state(
    conn: sqlite3.Connection,
    delivery_id: str,
    event_type: str,
    *,
    outcome: str,
    attributes: dict[str, str] | None = None,
) -> None:
    store = EventStore(conn)
    stream = store.read_stream("delivery", delivery_id)
    store.append(
        Event(
            event_type=event_type,
            stream_type="delivery",
            stream_id=delivery_id,
            producer="test",
            event_class=EventClass.DOMAIN,
            outcome=outcome,
            attributes=attributes or {},
        ),
        expected_version=len(stream),
    )
    conn.commit()


def _delivery_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])


class TestEventFirstDeliveryService:
    async def test_enqueue_creates_event_projection_and_protected_payload(
        self, conn, payload_store, clock
    ) -> None:
        service = _service(conn, payload_store, clock)
        delivery = await service.enqueue(
            DeliveryRequest(
                target={"channel": "qq", "to": "owner"},
                content_ref="message body",
            )
        )

        view = service.get(delivery.delivery_id)
        event = EventStore(conn).read_stream("delivery", delivery.delivery_id)[0]
        assert view is not None and view.status == "pending"
        assert view.target_snapshot["delivery_id"] == delivery.delivery_id
        assert event.attributes["effect_payload_kind"] == "delivery-effect.v2"
        assert event.payload_ref and event.payload_hash
        assert _delivery_rows(conn) == 0

    async def test_enqueue_idempotency_reuses_request_event(
        self, conn, payload_store, clock
    ) -> None:
        service = _service(conn, payload_store, clock)
        first = await service.enqueue(DeliveryRequest(target={}, content_ref="x", idempotency_key="idem"))
        second = await service.enqueue(DeliveryRequest(target={}, content_ref="y", idempotency_key="idem"))

        assert second.delivery_id == first.delivery_id
        assert len(EventStore(conn).read_stream("delivery", first.delivery_id)) == 1
        assert _delivery_rows(conn) == 0

    async def test_cancel_and_retry_append_lifecycle_events(self, conn, payload_store, clock) -> None:
        service = _service(conn, payload_store, clock)
        cancelled = await service.enqueue(DeliveryRequest(target={}, content_ref="x"))
        view = service.get(cancelled.delivery_id)
        assert view is not None
        await service.cancel(cancelled.delivery_id, view.stream_version)
        assert service.get(cancelled.delivery_id).status == "cancelled"  # type: ignore[union-attr]

        retried = await service.enqueue(DeliveryRequest(target={}, content_ref="y"))
        _append_delivery_state(
            conn,
            retried.delivery_id,
            "delivery.retry_scheduled",
            outcome="retry_scheduled",
        )
        view = service.get(retried.delivery_id)
        assert view is not None
        await service.retry(retried.delivery_id, view.stream_version)
        assert service.get(retried.delivery_id).status == "pending"  # type: ignore[union-attr]
        assert _delivery_rows(conn) == 0

    async def test_reconcile_unknown_and_completed_are_event_only(
        self, conn, payload_store, clock
    ) -> None:
        service = _service(conn, payload_store, clock)
        delivery = await service.enqueue(DeliveryRequest(target={}, content_ref="x"))
        _append_delivery_state(
            conn,
            delivery.delivery_id,
            "delivery.unknown",
            outcome="unknown",
            attributes={"platform_message_id": "pmid-1"},
        )

        result = await service.reconcile(
            delivery.delivery_id,
            platform_message_id="pmid-1",
            confirmed=True,
        )
        view = service.get(delivery.delivery_id)
        assert result.status == "sent"
        assert view is not None and view.status == "completed"
        assert view.platform_message_id == "pmid-1"
        assert _delivery_rows(conn) == 0

    async def test_canonical_effect_worker_completes_without_row_state(
        self, conn, payload_store, clock
    ) -> None:
        gateway = FakeGatewayClient(status="success", platform_message_id="pmid-ok")
        service = _service(conn, payload_store, clock, gateway)
        delivery = await service.enqueue(
            DeliveryRequest(target={"channel": "qq"}, content_ref="hello")
        )

        worker = CanonicalEffectWorker(
            EventStore(conn),
            CanonicalDeliveryEffectExecutor(payload_store, gateway),
            effect_types=frozenset({"delivery"}),
        )
        assert worker.run_pending() == 1
        view = service.get(delivery.delivery_id)
        assert view is not None and view.status == "completed"
        assert view.platform_message_id == "pmid-ok"
        assert len(view.receipts) == 1
        assert gateway.send_calls[0][1] == "hello"
        assert _delivery_rows(conn) == 0

    async def test_canonical_effect_worker_marks_unknown_without_row_state(
        self, conn, payload_store, clock
    ) -> None:
        gateway = FakeGatewayClient(status="unknown", error_code="gateway_timeout")
        service = _service(conn, payload_store, clock, gateway)
        delivery = await service.enqueue(DeliveryRequest(target={"channel": "qq"}, content_ref="hello"))

        worker = CanonicalEffectWorker(
            EventStore(conn),
            CanonicalDeliveryEffectExecutor(payload_store, gateway),
            effect_types=frozenset({"delivery"}),
        )
        assert worker.run_pending() == 1
        view = service.get(delivery.delivery_id)
        assert view is not None and view.status == "unknown"
        assert view.receipts[0]["receipt_kind"] == "unknown"
        assert _delivery_rows(conn) == 0
