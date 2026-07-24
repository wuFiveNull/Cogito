"""Verify the application initializes and the cutover tooling works on an Event-only schema.

This test creates a database with ONLY the event_log table (no legacy business tables)
and verifies:
1. EventStore append/read works
2. Event replay functions work
3. EventProjectionStore returns empty results (no crash)
4. assert_event_store_runtime_ready does NOT block (no legacy tables to check)
"""
from __future__ import annotations

import sqlite3
import time
import uuid

import pytest

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore
from cogito.store.event_replay import (
    replay_task,
    replay_turn,
    replay_run_attempt,
    replay_delivery,
    replay_memory,
    replay_connector,
    replay_schedule,
    replay_delegation,
)


@pytest.fixture
def event_only_db(tmp_path):
    """Create an Event-only database with no legacy business tables."""
    db_path = tmp_path / "event_only.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    # Create ONLY the event_log table — no other business tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_log (
            event_id          TEXT PRIMARY KEY,
            stream_type       TEXT NOT NULL,
            stream_id         TEXT NOT NULL,
            stream_version    INTEGER NOT NULL CHECK(stream_version > 0),
            event_type        TEXT NOT NULL,
            type_version      INTEGER NOT NULL DEFAULT 1 CHECK(type_version > 0),
            event_class       TEXT NOT NULL CHECK(event_class IN ('domain','operation','telemetry')),
            producer          TEXT NOT NULL,
            occurred_at       INTEGER NOT NULL,
            trace_id          TEXT NOT NULL DEFAULT '',
            span_id           TEXT NOT NULL DEFAULT '',
            parent_span_id    TEXT,
            correlation_id    TEXT NOT NULL DEFAULT '',
            causation_id      TEXT NOT NULL DEFAULT '',
            actor_id          TEXT NOT NULL DEFAULT '',
            principal_id      TEXT NOT NULL DEFAULT '',
            conversation_id   TEXT NOT NULL DEFAULT '',
            session_id        TEXT NOT NULL DEFAULT '',
            turn_id           TEXT NOT NULL DEFAULT '',
            attempt_id        TEXT NOT NULL DEFAULT '',
            task_id           TEXT NOT NULL DEFAULT '',
            summary           TEXT NOT NULL DEFAULT '',
            attributes_json   TEXT NOT NULL DEFAULT '{}',
            payload_ref       TEXT,
            payload_hash      TEXT NOT NULL DEFAULT '',
            outcome           TEXT NOT NULL DEFAULT '',
            error_category    TEXT NOT NULL DEFAULT '',
            idempotency_key   TEXT NOT NULL DEFAULT '',
            UNIQUE(stream_type, stream_id, stream_version)
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_event_log_idempotency
            ON event_log(producer, idempotency_key) WHERE idempotency_key <> ''
    """)
    yield conn
    conn.close()


def _append(store: EventStore, event_type: str, stream_type: str, stream_id: str, **kw):
    """Helper to append an Event with proper expected_version handling."""
    ver = kw.pop("expected_version", None)
    return store.append(
        Event(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            stream_type=stream_type,
            stream_id=stream_id,
            producer="test",
            event_class=EventClass.DOMAIN,
            occurred_at=int(time.time() * 1000),
            summary="test event",
            attributes=kw.get("attributes", {}),
            outcome=kw.get("outcome", "created"),
            idempotency_key=kw.get("idempotency_key", f"test:{event_type}:{stream_id}"),
        ),
        expected_version=ver,
    )


class TestEventOnlySchema:
    """Tests that run on a database with only the event_log table."""

    def test_event_store_append_and_read(self, event_only_db):
        store = EventStore(event_only_db)
        stream_id = uuid.uuid4().hex
        _append(store, "task.created", "task", stream_id)
        events = store.read_stream("task", stream_id)
        assert len(events) == 1
        assert events[0].event_type == "task.created"

    def test_event_replay_task(self, event_only_db):
        store = EventStore(event_only_db)
        tid = uuid.uuid4().hex
        _append(store, "task.created", "task", tid, outcome="created")
        _append(store, "task.completed", "task", tid, outcome="completed",
                idempotency_key=f"test:task.completed:{tid}", expected_version=1)
        events = store.read_stream("task", tid)
        proj = replay_task(events, tid)
        assert proj is not None
        assert proj.status == "completed"

    def test_event_replay_turn(self, event_only_db):
        store = EventStore(event_only_db)
        turn_id = uuid.uuid4().hex
        _append(store, "runtime.turn.accepted", "turn", turn_id, outcome="accepted",
                attributes={"session_id": "s1", "priority": 0})
        _append(store, "runtime.turn.completed", "turn", turn_id, outcome="completed",
                idempotency_key=f"test:turn.completed:{turn_id}", expected_version=1)
        events = store.read_stream("turn", turn_id)
        proj = replay_turn(events, turn_id)
        assert proj is not None
        assert proj.status == "completed"

    def test_event_replay_empty_stream(self, event_only_db):
        """Replaying an empty stream should return None."""
        events = []
        proj = replay_task(events, "nonexistent")
        assert proj is None

    def test_stream_version_conflict(self, event_only_db):
        store = EventStore(event_only_db)
        sid = uuid.uuid4().hex
        _append(store, "task.created", "task", sid, expected_version=0,
                idempotency_key=f"create:{sid}")
        # Reading back should have version 1
        events = store.read_stream("task", sid)
        assert len(events) == 1
        assert events[0].stream_version == 1
        # Appending with same expected_version but different idempotency should fail
        import pytest
        from cogito.store.event_store import StreamVersionConflictError
        with pytest.raises(StreamVersionConflictError):
            _append(store, "task.created", "task", sid, expected_version=0,
                    idempotency_key=f"create-dupe:{sid}")

    def test_event_projection_store_returns_empty(self, event_only_db):
        """EventProjectionStore should return empty lists on Event-only DB."""
        from cogito.store.event_projection_store import EventProjectionStore
        store = EventProjectionStore(EventStore(event_only_db))
        assert store.turns() == []
        assert store.tasks() == []
        assert store.deliveries() == []
        assert store.conversations() == []
        assert store.sessions() == []
        assert store.endpoints() == []
        assert store.principals() == []
        assert store.messages() == []
        assert store.attempts() == []
        assert store.connector_sources() == []
        assert store.memories() == []

    def test_read_stream_type_on_empty(self, event_only_db):
        """Reading stream types from empty DB should work."""
        store = EventStore(event_only_db)
        assert store.read_stream_type("task") == []
        assert store.read_stream_type("turn") == []
        assert store.read_stream_type("drift_run") == []

    def test_cutover_marker_not_present(self, event_only_db):
        """Event-only DB without cutover marker should not be marked as cutover."""
        from cogito.store.event_store_cutover import is_cutover_database
        assert not is_cutover_database(event_only_db)

    def test_assert_runtime_ready_passes(self, event_only_db):
        """Without a cutover marker, assert_event_store_runtime_ready should pass."""
        from cogito.store.event_store_cutover import assert_event_store_runtime_ready
        assert_event_store_runtime_ready(event_only_db)  # Should not raise
