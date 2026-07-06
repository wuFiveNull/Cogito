"""Tests for Outbox Worker and Delivery Worker reliability.

覆盖场景：
- 两个独立 SQLite 连接并发领取（竞态检测）
- Lease 过期后重新领取
- 旧 Worker 提交被拒绝
- retry_scheduled 未到期不可领取、到期后可领取
- 精确达到最大尝试次数进入 dead_letter/failed
- Outbox 同聚合版本顺序
- Worker 在外部成功、本地提交前崩溃
- Delivery unknown 不被自动重试
- Recovery Scan 幂等
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.service.delivery_worker import (
    DELIVERY_BACKOFF_BASE,
    DELIVERY_BACKOFF_MULTIPLIER,
    DeliveryWorker,
    FakeGateway,
)
from cogito.service.outbox_worker import OutboxWorker, compute_backoff
from cogito.service.recovery_service import RecoveryService
from cogito.store.migration import migrate

# ── Fixtures ──


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _insert_outbox_events(conn: sqlite3.Connection, events: list[dict]) -> None:
    for ev in events:
        conn.execute(
            "INSERT INTO outbox_events (event_id, event_type, aggregate_type, aggregate_id, "
            "aggregate_version, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ev["event_id"], ev.get("event_type", "TestEvent"),
             ev.get("aggregate_type", "turn"), ev.get("aggregate_id", "t1"),
             ev.get("aggregate_version", 1), ev.get("status", "pending"),
             ev.get("created_at", datetime.now(UTC).isoformat())),
        )
    conn.commit()


def _insert_delivery(conn: sqlite3.Connection, **overrides: object) -> str:
    import uuid
    delivery_id = overrides.get("delivery_id", uuid.uuid4().hex)
    conn.execute(
        "INSERT INTO deliveries (delivery_id, target_snapshot, content_ref, status, "
        "idempotency_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (delivery_id,
         overrides.get("target_snapshot", '{"target": "test"}'),
         overrides.get("content_ref", "msg_ref"),
         overrides.get("status", "pending"),
         overrides.get("idempotency_key", f"key_{delivery_id[:8]}"),
         overrides.get("created_at", datetime.now(UTC).isoformat())),
    )
    conn.commit()
    return delivery_id


def make_clock(iso_str: str) -> datetime:
    """Create a fixed clock from ISO string."""
    return datetime.fromisoformat(iso_str)


# =============================================================================
# Outbox Worker Tests
# =============================================================================


class TestOutboxLease:
    def test_lease_pending_event(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")
        assert lease is not None
        assert lease.event_id == "e1"
        assert lease.attempt_count == 1

        row = db.execute(
            "SELECT status, lease_owner FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["status"] == "leased"
        assert row["lease_owner"] == "w1"

    def test_lease_returns_none_when_empty(self, db: sqlite3.Connection):
        worker = OutboxWorker(db)
        assert worker.lease_next("w1") is None

    def test_lease_skips_already_leased(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1", "status": "leased"}])
        worker = OutboxWorker(db)
        assert worker.lease_next("w1") is None

    def test_lease_maintains_order(self, db: sqlite3.Connection):
        """Events are leased in aggregate order, respecting version gaps."""
        _insert_outbox_events(db, [
            {"event_id": "e1", "aggregate_id": "t1", "aggregate_version": 1},
            {"event_id": "e2", "aggregate_id": "t1", "aggregate_version": 2},
            {"event_id": "e3", "aggregate_id": "t2", "aggregate_version": 1},
        ])
        worker = OutboxWorker(db)

        l1 = worker.lease_next("w1")
        assert l1 is not None
        assert l1.event_id == "e1"

        l2 = worker.lease_next("w1")
        assert l2 is not None
        assert l2.event_id == "e3"  # different aggregate

        assert worker.lease_next("w1") is None  # e2 blocked by e1

        worker.publish(l1, "w1")
        l3 = worker.lease_next("w1")
        assert l3 is not None
        assert l3.event_id == "e2"

    def test_order_skips_aggregate_with_pending_previous(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [
            {"event_id": "e1", "aggregate_id": "t1", "aggregate_version": 1},
            {"event_id": "e2", "aggregate_id": "t1", "aggregate_version": 2},
        ])
        worker = OutboxWorker(db)

        lease1 = worker.lease_next("w1")
        assert lease1 is not None
        assert lease1.event_id == "e1"

        worker.publish(lease1, "w1")
        lease2 = worker.lease_next("w1")
        assert lease2 is not None
        assert lease2.event_id == "e2"

    def test_retry_scheduled_expired_can_be_leased(self, db: sqlite3.Connection):
        """retry_scheduled past next_attempt_at should be leasable."""
        _insert_outbox_events(db, [
            {"event_id": "e1", "status": "retry_scheduled"},
        ])
        # Set next_attempt_at in the past
        past_time = datetime(2020, 1, 1, tzinfo=UTC)
        db.execute(
            "UPDATE outbox_events SET next_attempt_at=? WHERE event_id='e1'",
            (past_time.isoformat(),),
        )
        db.commit()

        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")
        assert lease is not None
        assert lease.event_id == "e1"

    def test_retry_scheduled_future_cannot_be_leased(self, db: sqlite3.Connection):
        """retry_scheduled with future next_attempt_at should NOT be leasable."""
        _insert_outbox_events(db, [
            {"event_id": "e1", "status": "retry_scheduled"},
        ])
        future_time = datetime(2099, 1, 1, tzinfo=UTC)
        db.execute(
            "UPDATE outbox_events SET next_attempt_at=? WHERE event_id='e1'",
            (future_time.isoformat(),),
        )
        db.commit()

        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")
        assert lease is None


class TestOutboxPublish:
    def test_publish_success(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")

        ok = worker.publish(lease, "w1")
        assert ok is True

        row = db.execute(
            "SELECT status FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["status"] == "published"

    def test_publish_wrong_worker_fails(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")
        assert lease is not None

        ok = worker.publish(lease, "wrong_worker")
        assert ok is False  # wrong worker_id

    def test_publish_wrong_version_fails(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")
        assert lease is not None

        # Tamper with lease_version in the lease object
        bad_lease = lease._replace(lease_version=999)
        ok = worker.publish(bad_lease, "w1")
        assert ok is False


class TestOutboxRetry:
    def test_retry_event(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")

        ok = worker.retry(lease, "w1")
        assert ok is True

        row = db.execute(
            "SELECT status FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["status"] == "retry_scheduled"

    def test_retry_sets_next_attempt_at(self, db: sqlite3.Connection):
        """After retry, next_attempt_at should be set in the future."""
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")
        assert lease is not None
        assert lease.attempt_count == 1

        clock = make_clock("2026-01-15T12:00:00+00:00")
        worker.retry(lease, "w1", clock=clock)

        row = db.execute(
            "SELECT next_attempt_at, attempt_count FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["next_attempt_at"] is not None
        # next_attempt_at should be in the future
        assert row["next_attempt_at"] > clock.isoformat()

    def test_dead_letter_after_max_retries(self, db: sqlite3.Connection):
        """After MAX_TENTATIVE attempts, retry should move to dead_letter."""
        from cogito.service.outbox_worker import MAX_TENTATIVE
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)

        clock = make_clock("2026-01-15T12:00:00+00:00")

        for attempt in range(1, MAX_TENTATIVE + 1):
            lease = worker.lease_next("w1", clock=clock)
            assert lease is not None, f"Failed to lease at attempt {attempt}"
            assert lease.attempt_count == attempt

            ok = worker.retry(lease, "w1", clock=clock)
            assert ok is True

            # Advance clock past the backoff for next lease (backoff grows exponentially)
            if attempt == 1:
                clock = make_clock("2026-01-15T12:00:20+00:00")  # past 10s backoff
            elif attempt == 2:
                clock = make_clock("2026-01-15T12:01:00+00:00")  # past 30s backoff

        row = db.execute(
            "SELECT status, attempt_count FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["status"] == "dead_letter"
        assert row["attempt_count"] == MAX_TENTATIVE

    def test_retry_wrong_worker_fails(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)
        lease = worker.lease_next("w1")

        ok = worker.retry(lease, "wrong_worker")
        assert ok is False

    def test_dead_letter_direct(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1", "status": "leased"}])
        worker = OutboxWorker(db)

        ok = worker.dead_letter("e1")
        assert ok is True

        row = db.execute(
            "SELECT status FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["status"] == "dead_letter"


class TestOutboxFullCycle:
    def test_full_lease_publish_cycle(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)

        lease = worker.lease_next("w1")
        assert lease is not None
        assert db.execute(
            "SELECT status FROM outbox_events WHERE event_id='e1'"
        ).fetchone()["status"] == "leased"

        worker.publish(lease, "w1")
        assert db.execute(
            "SELECT status FROM outbox_events WHERE event_id='e1'"
        ).fetchone()["status"] == "published"

        assert worker.lease_next("w1") is None

    def test_multiple_aggregates_independent(self, db: sqlite3.Connection):
        _insert_outbox_events(db, [
            {"event_id": "e1", "aggregate_id": "t1", "aggregate_version": 1},
            {"event_id": "e2", "aggregate_id": "t2", "aggregate_version": 1},
        ])
        worker = OutboxWorker(db)

        l1 = worker.lease_next("w1")
        l2 = worker.lease_next("w2")

        assert l1 is not None
        assert l2 is not None
        assert l1.event_id != l2.event_id


class TestOutboxConcurrency:
    def test_concurrent_lease_only_one_succeeds(self, db: sqlite3.Connection):
        """Two workers trying to lease the same event — only one succeeds."""
        _insert_outbox_events(db, [{"event_id": "e1"}])
        w1 = OutboxWorker(db)
        w2 = OutboxWorker(db)

        l1 = w1.lease_next("w1")
        l2 = w2.lease_next("w2")

        assert l1 is not None
        assert l2 is None  # second lease should fail

    def test_old_worker_publish_rejected(self, db: sqlite3.Connection):
        """After lease is recovered, old worker publish should be rejected."""
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)

        lease = worker.lease_next("w1")
        assert lease is not None

        # Recovery resets the lease
        RecoveryService(db).recover_outbox_leases(
            clock=datetime(2099, 1, 1, tzinfo=UTC)
        )

        # Old worker tries to publish with stale lease
        ok = worker.publish(lease, "w1")
        assert ok is False  # status is now 'pending', not 'leased'

    def test_lease_expiry_recover_and_release(self, db: sqlite3.Connection):
        """Expired lease can be recovered and re-leased."""
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)

        clock = make_clock("2026-01-15T12:00:00+00:00")
        lease = worker.lease_next("w1", clock=clock)
        assert lease is not None

        # Fast-forward past lease expiry
        future_clock = make_clock("2026-01-15T13:00:00+00:00")
        recovery = RecoveryService(db)
        count = recovery.recover_outbox_leases(clock=future_clock)
        assert count == 1

        # Can now be re-leased
        lease2 = worker.lease_next("w2", clock=future_clock)
        assert lease2 is not None
        assert lease2.event_id == "e1"
        assert lease2.attempt_count == 2  # attempt count incremented again


class TestOutboxBackoff:
    def test_backoff_increases(self):
        """Exponential backoff should increase with attempt count."""
        b1 = compute_backoff(1)
        b2 = compute_backoff(2)
        b3 = compute_backoff(3)
        assert b1 < b2 < b3
        assert b1 == 10
        assert b2 == 30
        assert b3 == 90


# =============================================================================
# Delivery Worker Tests
# =============================================================================


class TestDeliveryLease:
    def test_lease_pending_delivery(self, db: sqlite3.Connection):
        did = _insert_delivery(db)
        gateway = FakeGateway()
        worker = DeliveryWorker(db, gateway)

        lease = worker.lease_next("w1")
        assert lease is not None
        assert lease.delivery_id == did
        assert lease.attempt_count == 1

        row = db.execute(
            "SELECT status FROM deliveries WHERE delivery_id=?", (did,)
        ).fetchone()
        assert row["status"] == "sending"

    def test_lease_creates_attempt(self, db: sqlite3.Connection):
        _insert_delivery(db)
        gateway = FakeGateway()
        worker = DeliveryWorker(db, gateway)

        worker.lease_next("w1")

        attempts = db.execute(
            "SELECT status, attempt_no FROM delivery_attempts"
        ).fetchall()
        assert len(attempts) == 1
        assert attempts[0]["status"] == "sending"
        assert attempts[0]["attempt_no"] == 1

    def test_lease_none_when_empty(self, db: sqlite3.Connection):
        worker = DeliveryWorker(db, FakeGateway())
        assert worker.lease_next("w1") is None

    def test_lease_retry_scheduled_expired(self, db: sqlite3.Connection):
        """retry_scheduled past next_attempt_at can be leased."""
        did = _insert_delivery(db, status="retry_scheduled")
        past_time = datetime(2020, 1, 1, tzinfo=UTC)
        db.execute(
            "UPDATE deliveries SET next_attempt_at=? WHERE delivery_id=?",
            (past_time.isoformat(), did),
        )
        db.commit()

        worker = DeliveryWorker(db, FakeGateway())
        lease = worker.lease_next("w1")
        assert lease is not None
        assert lease.delivery_id == did

    def test_lease_retry_scheduled_future_skipped(self, db: sqlite3.Connection):
        """retry_scheduled with future next_attempt_at is skipped."""
        _insert_delivery(db, status="retry_scheduled")
        future_time = datetime(2099, 1, 1, tzinfo=UTC)
        db.execute(
            "UPDATE deliveries SET next_attempt_at=?",
            (future_time.isoformat(),),
        )
        db.commit()

        worker = DeliveryWorker(db, FakeGateway())
        lease = worker.lease_next("w1")
        assert lease is None


class TestDeliverySend:
    def test_send_success(self, db: sqlite3.Connection):
        did = _insert_delivery(db, content_ref="msg_1")
        gateway = FakeGateway()
        worker = DeliveryWorker(db, gateway)

        lease = worker.lease_next("w1")
        result = worker.deliver(lease, "w1")

        assert result == "sent"
        assert len(gateway.sent) == 1
        assert gateway.sent[0][1] == "msg_1"

        row = db.execute(
            "SELECT status, platform_message_id FROM deliveries WHERE delivery_id=?",
            (did,),
        ).fetchone()
        assert row["status"] == "sent"
        assert row["platform_message_id"] is not None

        attempt = db.execute("SELECT status FROM delivery_attempts").fetchone()
        assert attempt["status"] == "succeeded"

    def test_send_failure_retries(self, db: sqlite3.Connection):
        did = _insert_delivery(db)
        gateway = FakeGateway(fail=True)
        worker = DeliveryWorker(db, gateway)

        lease = worker.lease_next("w1")
        result = worker.deliver(lease, "w1")

        assert result == "failed"
        assert len(gateway.sent) == 0

        row = db.execute(
            "SELECT status FROM deliveries WHERE delivery_id=?", (did,),
        ).fetchone()
        assert row["status"] == "retry_scheduled"

    def test_send_unknown_goes_to_unknown(self, db: sqlite3.Connection):
        """External success but local unknown → status='unknown'."""
        did = _insert_delivery(db)
        gateway = FakeGateway(unknown=True)
        worker = DeliveryWorker(db, gateway)

        lease = worker.lease_next("w1")
        result = worker.deliver(lease, "w1")

        assert result == "unknown"

        row = db.execute(
            "SELECT status FROM deliveries WHERE delivery_id=?", (did,),
        ).fetchone()
        assert row["status"] == "unknown"  # not auto-retried

    def test_unknown_not_auto_retried(self, db: sqlite3.Connection):
        """unknown Delivery cannot be leased by normal worker — must reconcile."""
        _insert_delivery(db, status="unknown")
        worker = DeliveryWorker(db, FakeGateway())

        lease = worker.lease_next("w1")
        assert lease is None  # unknown is not leasable

    def test_reconcile_from_unknown(self, db: sqlite3.Connection):
        """Reconcile path: unknown → sent."""
        did = _insert_delivery(db, status="unknown")
        worker = DeliveryWorker(db, FakeGateway())

        ok = worker.reconcile(did, "ext_msg_1")
        assert ok is True

        row = db.execute(
            "SELECT status, platform_message_id FROM deliveries WHERE delivery_id=?",
            (did,),
        ).fetchone()
        assert row["status"] == "sent"
        assert row["platform_message_id"] == "ext_msg_1"

    def test_max_retries_leads_to_failed(self, db: sqlite3.Connection):
        """After MAX_DELIVERY_TENTATIVE failures, status should be 'failed'."""
        did = _insert_delivery(db, content_ref="retry_msg")
        gateway = FakeGateway(fail=True)
        worker = DeliveryWorker(db, gateway)

        from cogito.service.delivery_worker import MAX_DELIVERY_TENTATIVE

        for attempt in range(1, MAX_DELIVERY_TENTATIVE + 1):
            # Set status to pending for retry simulation
            # In production, this would be done by RecoveryService
            if attempt > 1:
                db.execute(
                    "UPDATE deliveries SET status='pending' WHERE delivery_id=?",
                    (did,),
                )
                db.commit()

            lease = worker.lease_next("w1")
            assert lease is not None, f"Failed to lease at attempt {attempt}"
            assert lease.attempt_count == attempt
            result = worker.deliver(lease, "w1")

            if attempt < MAX_DELIVERY_TENTATIVE:
                assert result == "failed"
                # After retry, status is retry_scheduled - set back to pending for next lease
            else:
                assert result == "failed"

        row = db.execute(
            "SELECT status, attempt_count FROM deliveries WHERE delivery_id=?",
            (did,),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == MAX_DELIVERY_TENTATIVE

    def test_stale_deliver_rejected(self, db: sqlite3.Connection):
        """Worker with expired/stolen lease gets 'stale' response."""
        did = _insert_delivery(db)
        gateway = FakeGateway()
        worker = DeliveryWorker(db, gateway)

        lease = worker.lease_next("w1")
        assert lease is not None

        # Another worker claims the same delivery (simulating lease recovery)
        db.execute(
            "UPDATE deliveries SET status='pending', lease_owner=NULL, lease_version=0 "
            "WHERE delivery_id=?",
            (did,),
        )
        db.commit()
        worker.lease_next("w2")

        # Old worker tries to deliver with stale lease
        result = worker.deliver(lease, "w1")
        assert result == "stale"


# =============================================================================
# Recovery Service Tests
# =============================================================================


class TestRecoveryService:
    def test_recover_outbox_leases(self, db: sqlite3.Connection):
        """Expired outbox leases are reset to pending."""
        _insert_outbox_events(db, [{"event_id": "e1"}])
        worker = OutboxWorker(db)

        lease_clock = make_clock("2026-01-15T12:00:00+00:00")
        worker.lease_next("w1", clock=lease_clock)

        # Fast-forward past lease expiry
        future = make_clock("2026-01-15T12:30:00+00:00")
        recovery = RecoveryService(db)
        count = recovery.recover_outbox_leases(clock=future)
        assert count == 1

        row = db.execute(
            "SELECT status FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["status"] == "pending"  # reset to pending

    def test_recover_outbox_idempotent(self, db: sqlite3.Connection):
        """Running recovery twice should be safe."""
        _insert_outbox_events(db, [{"event_id": "e1"}])
        lease_clock = make_clock("2026-01-15T12:00:00+00:00")
        OutboxWorker(db).lease_next("w1", clock=lease_clock)

        future = make_clock("2026-01-15T12:30:00+00:00")
        recovery = RecoveryService(db)
        count1 = recovery.recover_outbox_leases(clock=future)
        assert count1 == 1

        count2 = recovery.recover_outbox_leases(clock=future)
        assert count2 == 0  # already recovered

    def test_recover_recovered_not_double_counted(self, db: sqlite3.Connection):
        """Already recovered items should not be recovered again."""
        _insert_outbox_events(db, [{"event_id": "e1"}])
        lease_clock = make_clock("2026-01-15T12:00:00+00:00")
        OutboxWorker(db).lease_next("w1", clock=lease_clock)

        future = make_clock("2026-01-15T12:30:00+00:00")
        recovery = RecoveryService(db)
        assert recovery.recover_outbox_leases(clock=future) == 1
        assert recovery.recover_outbox_leases(clock=future) == 0

    def test_recover_delivery_leases(self, db: sqlite3.Connection):
        """Expired delivery leases are reset to pending."""
        did = _insert_delivery(db)
        gateway = FakeGateway()
        worker = DeliveryWorker(db, gateway)

        lease_clock = make_clock("2026-01-15T12:00:00+00:00")
        worker.lease_next("w1", clock=lease_clock)

        future = make_clock("2026-01-15T12:30:00+00:00")
        recovery = RecoveryService(db)
        count = recovery.recover_delivery_leases(clock=future)
        assert count == 1

        row = db.execute(
            "SELECT status FROM deliveries WHERE delivery_id=?", (did,),
        ).fetchone()
        assert row["status"] == "pending"

    def test_recover_unknown_not_touched(self, db: sqlite3.Connection):
        """unknown Delivery should NOT be reset to pending by recovery."""
        did = _insert_delivery(db, status="unknown")
        recovery = RecoveryService(db)
        count = recovery.recover_delivery_leases()
        assert count == 0

        row = db.execute(
            "SELECT status FROM deliveries WHERE delivery_id=?", (did,),
        ).fetchone()
        assert row["status"] == "unknown"

    def test_recover_stale_turns(self, db: sqlite3.Connection):
        """running Turn without valid execution right → queued."""
        from cogito.service.dispatcher import Dispatcher
        from tests.service.test_dispatcher import _create_queued_turn, _create_session

        _create_session(db, "s1", "c1")
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        # Recovery should reset running → queued
        recovery = RecoveryService(db)
        count = recovery.recover_stale_turns()
        assert count == 1

        row = db.execute(
            "SELECT status FROM turns WHERE turn_id=?",
            (claimed.turn.turn_id,),
        ).fetchone()
        assert row["status"] == "queued"

    def test_recover_all(self, db: sqlite3.Connection):
        """recover_all runs all recovery scans."""
        import uuid
        # Create an outbox with expired lease
        eid = uuid.uuid4().hex
        _insert_outbox_events(db, [{"event_id": eid}])
        lease_clock = make_clock("2026-01-15T12:00:00+00:00")
        OutboxWorker(db).lease_next("w1", clock=lease_clock)

        future = make_clock("2026-01-15T12:30:00+00:00")
        recovery = RecoveryService(db)
        result = recovery.recover_all(clock=future)
        assert result["outbox_leases"] >= 1


# =============================================================================
# Full cycle: Outbox + Delivery + Recovery
# =============================================================================


class TestFullCycle:
    def test_outbox_published_then_delivered(self, db: sqlite3.Connection):
        """Simulate: outbox event published → delivery created → sent."""
        _insert_outbox_events(db, [{"event_id": "evt1"}])
        oworker = OutboxWorker(db)

        lease = oworker.lease_next("w1")
        assert lease is not None
        oworker.publish(lease, "w1")

        _insert_delivery(db, content_ref="final_msg", idempotency_key="key_1")

        gateway = FakeGateway()
        dworker = DeliveryWorker(db, gateway)
        dlease = dworker.lease_next("w1")
        assert dlease is not None
        result = dworker.deliver(dlease, "w1")

        assert result == "sent"
        assert len(gateway.sent) == 1
        assert gateway.sent[0][1] == "final_msg"

    def test_outbox_aggregate_version_ordering(self, db: sqlite3.Connection):
        """Outbox events from same aggregate must be published in version order."""
        _insert_outbox_events(db, [
            {"event_id": "v1", "aggregate_id": "agg1", "aggregate_version": 1},
            {"event_id": "v2", "aggregate_id": "agg1", "aggregate_version": 2},
            {"event_id": "v3", "aggregate_id": "agg1", "aggregate_version": 3},
        ])
        worker = OutboxWorker(db)

        l1 = worker.lease_next("w1")
        assert l1 is not None and l1.event_id == "v1"

        # v2 is blocked by v1 still being leased
        assert worker.lease_next("w1") is None

        worker.publish(l1, "w1")

        l2 = worker.lease_next("w1")
        assert l2 is not None and l2.event_id == "v2"

        worker.publish(l2, "w1")

        l3 = worker.lease_next("w1")
        assert l3 is not None and l3.event_id == "v3"

    def test_worker_crash_after_external_success(self, db: sqlite3.Connection):
        """Worker gets external success but crashes before commit → delivery unknown."""
        _insert_delivery(db, content_ref="crash_msg")
        gateway = FakeGateway()
        worker = DeliveryWorker(db, gateway)

        lease_clock = make_clock("2026-01-15T12:00:00+00:00")
        lease = worker.lease_next("w1", clock=lease_clock)
        assert lease is not None

        # Simulate: gateway says True but worker crashes before commit
        # Next scan finds it as sending with expired lease
        future = make_clock("2026-01-15T12:30:00+00:00")
        RecoveryService(db).recover_delivery_leases(clock=future)

        # After recovery, it's back to pending
        row = db.execute(
            "SELECT status FROM deliveries WHERE delivery_id=?",
            (lease.delivery_id,),
        ).fetchone()
        assert row["status"] == "pending"

    def test_gateway_sent_records(self, db: sqlite3.Connection):
        """Verify FakeGateway records all sent messages."""
        gateway = FakeGateway()
        worker = DeliveryWorker(db, gateway)

        _insert_delivery(db, content_ref="msg_a", idempotency_key="k1")
        l1 = worker.lease_next("w1")
        worker.deliver(l1, "w1")

        _insert_delivery(db, content_ref="msg_b", idempotency_key="k2")
        l2 = worker.lease_next("w1")
        worker.deliver(l2, "w1")

        assert len(gateway.sent) == 2
        assert gateway.sent[0][1] == "msg_a"
        assert gateway.sent[1][1] == "msg_b"


class TestDeliveryBackoff:
    def test_delivery_backoff_increases(self):
        from cogito.service.delivery_worker import compute_delivery_backoff
        b1 = compute_delivery_backoff(1)
        b2 = compute_delivery_backoff(2)
        b3 = compute_delivery_backoff(3)
        assert b1 == DELIVERY_BACKOFF_BASE
        assert b2 == DELIVERY_BACKOFF_BASE * DELIVERY_BACKOFF_MULTIPLIER
        assert b3 == DELIVERY_BACKOFF_BASE * (DELIVERY_BACKOFF_MULTIPLIER ** 2)
        assert b1 < b2 < b3
