"""Tests for accept_inbound application service (P2 core transaction)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread

import pytest

from cogito.contracts.envelope import ChannelEnvelope
from cogito.service.inbound_service import InboundService
from cogito.store.event_store import EventStore

# ── Helper ──


def _envelope(**overrides: object) -> ChannelEnvelope:
    """Helper to create a ChannelEnvelope with defaults."""
    data = {
        "channel_type": "test_channel",
        "channel_instance_id": "ci1",
        "platform_sender_id": "sender1",
        "platform_conversation_id": "conv1",
        "platform_message_id": "pm1",
        "content_parts": [{"content_type": "text", "inline_data": "Hello"}],
        "trust_label": "unverified",
        "received_at": datetime.now(UTC).isoformat(),
    }
    data.update(overrides)
    return ChannelEnvelope(**data)


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    """Count rows in a legacy table. Only use for tables not yet Event-sourced."""
    _ALLOWED_LEGACY_COUNT = frozenset({"inbound_inbox", "outbox_events"})
    if table not in _ALLOWED_LEGACY_COUNT:
        # For Event-sourced tables, count events instead
        stream_type_map = {
            "principals": "principal", "endpoints": "endpoint",
            "conversations": "conversation", "sessions": "session",
            "messages": "message", "turns": "turn",
            "run_attempts": "run_attempt", "tasks": "task",
            "task_attempts": "task_attempt", "deliveries": "delivery",
            "model_calls": "model_call", "tool_calls": "tool_call",
            "approvals": "approval", "content_parts": "message",
        }
        s = stream_type_map.get(table)
        if s:
            return len({e.stream_id for e in EventStore(conn).read_stream_type(s)})
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _setup_in_memory_db() -> sqlite3.Connection:
    """Create a fresh in-memory database with schema."""
    from cogito.store.migration import migrate

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


# ── Fixtures ──


@pytest.fixture
def service(in_memory_db: sqlite3.Connection, tmp_path: Path) -> InboundService:
    from cogito.infrastructure.payload_store import PayloadStore

    return InboundService(in_memory_db, payload_store=PayloadStore(tmp_path / "payloads", in_memory_db))


@pytest.fixture
def conn(in_memory_db: sqlite3.Connection) -> sqlite3.Connection:
    return in_memory_db


# =============================================================================
# Test Case 1: 首次文本入站
# =============================================================================


class TestFirstInbound:
    def test_creates_message_and_turn(self, service: InboundService, conn: sqlite3.Connection):
        result = service.accept(_envelope())

        assert result.is_new is True
        assert result.message_id != ""
        assert result.turn_id != ""

        # Message exists — verified via Event
        from cogito.store.event_replay import replay_message

        msg_state = replay_message(
            EventStore(conn).read_stream("message", result.message_id), result.message_id
        )
        assert msg_state is not None
        assert msg_state.role == "user"
        assert msg_state.direction == "inbound"
        assert msg_state.receive_sequence == 1

        # Turn is rebuilt from its canonical accepted → queued Event stream.
        from cogito.store.event_replay import replay_turn

        turn = replay_turn(EventStore(conn).read_stream("turn", result.turn_id), result.turn_id)
        assert turn is not None
        assert turn.status == "queued"
        assert turn.stream_version == 2  # accepted(1) → queued(2)
        assert turn.input_message_id == result.message_id

    def test_creates_content_parts(self, service: InboundService, conn: sqlite3.Connection):
        result = service.accept(
            _envelope(
                content_parts=[
                    {"content_type": "text", "inline_data": "Part 1"},
                    {"content_type": "text", "inline_data": "Part 2"},
                ],
            )
        )

        # Verify via message event descriptors
        msg_events = EventStore(conn).read_stream("message", result.message_id)
        recorded = [e for e in msg_events if e.event_type == "interaction.message.recorded"]
        assert len(recorded) == 1
        descriptors = recorded[0].attributes.get("part_descriptors", [])
        assert len(descriptors) == 2

    def test_creates_inbox_record(self, service: InboundService, conn: sqlite3.Connection):
        result = service.accept(_envelope(channel_instance_id="ci1", platform_message_id="pm1"))

        # Inbox dedup is via Event idempotency key
        found = EventStore(conn).find_idempotent(
            "channel:test_channel",
            f"inbound-message:ci1:pm1",
        )
        assert found is not None
        assert found.event_type == "interaction.message.accepted"

    def test_creates_canonical_events(self, service: InboundService, conn: sqlite3.Connection):
        result = service.accept(_envelope())

        events = [
            event
            for event in EventStore(conn).list_events(limit=20)
            if event.event_type in {"interaction.message.accepted", "runtime.turn.queued"}
        ]
        assert len(events) == 2
        events_by_type = {event.event_type: event for event in events}
        accepted = events_by_type["interaction.message.accepted"]
        queued = events_by_type["runtime.turn.queued"]
        assert accepted.stream_id == result.message_id
        assert accepted.context.turn_id == result.turn_id
        assert accepted.context.trace_id
        assert queued.stream_id == result.turn_id
        assert queued.context.trace_id == accepted.context.trace_id
        turn_accepted = EventStore(conn).get(queued.context.causation_id)
        assert turn_accepted is not None
        assert turn_accepted.event_type == "runtime.turn.accepted"
        assert turn_accepted.context.causation_id == accepted.event_id
        assert _count_rows(conn, "outbox_events") == 0

    def test_creates_principal_and_endpoint(
        self, service: InboundService, conn: sqlite3.Connection
    ):
        service.accept(
            _envelope(
                channel_type="tg",
                channel_instance_id="tg1",
                platform_sender_id="user_a",
            )
        )

        # Verify via Event replay
        from cogito.store.event_replay import replay_principal

        principals = EventStore(conn).read_stream_type("principal")
        assert len({e.stream_id for e in principals}) == 1
        principal_id = next(iter({e.stream_id for e in principals}))
        p = replay_principal(principals, principal_id)
        assert p is not None
        assert p.principal_type == "external_user"

        endpoints = EventStore(conn).read_stream_type("endpoint")
        assert len({e.stream_id for e in endpoints}) == 1
        endpoint_id = next(iter({e.stream_id for e in endpoints}))
        from cogito.store.event_replay import replay_endpoint

        ep = replay_endpoint(endpoints, endpoint_id)
        assert ep is not None
        assert ep.channel_type == "tg"
        assert ep.platform_account_id == "user_a"

    def test_creates_conversation_and_session(
        self, service: InboundService, conn: sqlite3.Connection
    ):
        service.accept(
            _envelope(
                channel_instance_id="ci1",
                platform_sender_id="user_a",
                platform_conversation_id="gc1",
            )
        )

        # Verify via Event replay
        from cogito.store.event_replay import replay_conversation, replay_session

        conversations = EventStore(conn).read_stream_type("conversation")
        assert len({e.stream_id for e in conversations}) == 1
        cid = next(iter({e.stream_id for e in conversations}))
        conv = replay_conversation(conversations, cid)
        assert conv is not None
        assert conv.conversation_type == "private"
        assert conv.platform_conversation_id == "gc1"

        sessions = EventStore(conn).read_stream_type("session")
        assert len({e.stream_id for e in sessions}) == 1
        sid = next(iter({e.stream_id for e in sessions}))
        sess = replay_session(sessions, sid)
        assert sess is not None
        assert sess.status == "active"


# =============================================================================
# Test Case 2: 同一事件重复提交 → 幂等
# =============================================================================


class TestIdempotentDedup:
    def test_returns_same_ids(self, service: InboundService, conn: sqlite3.Connection):
        r1 = service.accept(_envelope())
        r2 = service.accept(_envelope())

        assert r2.is_new is False
        assert r2.message_id == r1.message_id
        assert r2.turn_id == r1.turn_id

    def test_does_not_create_extra_rows(self, service: InboundService, conn: sqlite3.Connection):
        service.accept(_envelope())
        count1 = _count_rows(conn, "messages")

        service.accept(_envelope())
        count2 = _count_rows(conn, "messages")

        assert count2 == count1  # no extra message

    def test_turn_not_duplicated(self, service: InboundService, conn: sqlite3.Connection):
        service.accept(_envelope())
        count1 = _count_rows(conn, "turns")

        service.accept(_envelope())
        count2 = _count_rows(conn, "turns")

        assert count2 == count1  # no extra turn

    def test_events_not_duplicated(self, service: InboundService, conn: sqlite3.Connection):
        service.accept(_envelope())
        count1 = len(EventStore(conn).list_events(limit=10))

        service.accept(_envelope())
        count2 = len(EventStore(conn).list_events(limit=10))

        assert count2 == count1  # no extra events


# =============================================================================
# Test Case 3: 相同文本但不同平台事件不被误去重
# =============================================================================


class TestDifferentPlatformEvents:
    def test_different_events_both_create(self, service: InboundService, conn: sqlite3.Connection):
        r1 = service.accept(
            _envelope(
                channel_instance_id="ci1",
                platform_message_id="pm1",
                content_parts=[{"content_type": "text", "inline_data": "Same text"}],
            )
        )
        r2 = service.accept(
            _envelope(
                channel_instance_id="ci1",
                platform_message_id="pm2",
                content_parts=[{"content_type": "text", "inline_data": "Same text"}],
            )
        )

        # Both should be new since platform_event_ids differ
        assert r1.is_new is True
        assert r2.is_new is True
        assert r1.message_id != r2.message_id

    def test_different_channels_same_event_id(
        self, service: InboundService, conn: sqlite3.Connection
    ):
        r1 = service.accept(_envelope(channel_instance_id="ci1", platform_message_id="pm1"))
        r2 = service.accept(_envelope(channel_instance_id="ci2", platform_message_id="pm1"))

        assert r1.is_new is True
        assert r2.is_new is True  # different channel, so different inbox partition


# =============================================================================
# Test Case 4: Conversation/Session 复用与 receive_sequence 单调递增
# =============================================================================


class TestConversationReuse:
    def test_reuses_conversation(self, service: InboundService, conn: sqlite3.Connection):
        r1 = service.accept(
            _envelope(
                channel_instance_id="ci1",
                platform_sender_id="user_a",
                platform_conversation_id="gc1",
            )
        )
        r2 = service.accept(
            _envelope(
                channel_instance_id="ci1",
                platform_sender_id="user_a",
                platform_conversation_id="gc1",
                platform_message_id="pm2",
            )
        )

        assert r1.is_new is True
        assert r2.is_new is True

        cv_events = EventStore(conn).read_stream_type("conversation")
        assert len({e.stream_id for e in cv_events}) == 1  # one conversation reused

    def test_sequence_monotonic(self, service: InboundService, conn: sqlite3.Connection):
        service.accept(_envelope(platform_message_id="pm1"))
        service.accept(_envelope(platform_message_id="pm2"))
        service.accept(_envelope(platform_message_id="pm3"))

        from cogito.store.event_replay import replay_message

        seen_ids = set()
        seqs = []
        for event in EventStore(conn).read_stream_type("message"):
            if event.stream_id not in seen_ids:
                seen_ids.add(event.stream_id)
                state = replay_message(EventStore(conn).read_stream("message", event.stream_id), event.stream_id)
                if state is not None:
                    seqs.append(state.receive_sequence)
        seqs.sort()
        assert seqs == [1, 2, 3]

    def test_reuses_session(self, service: InboundService, conn: sqlite3.Connection):
        service.accept(_envelope(platform_message_id="pm1"))
        service.accept(_envelope(platform_message_id="pm2"))

        ses_events = EventStore(conn).read_stream_type("session")
        assert len({e.stream_id for e in ses_events}) == 1  # one session reused


# =============================================================================
# Test Case 5: ContentPart 多段内容
# =============================================================================


class TestMultiPartContent:
    def test_mixed_content_types(self, service: InboundService, conn: sqlite3.Connection):
        parts = [
            {"content_type": "text", "inline_data": "Hello"},
            {"content_type": "image", "payload_ref": "obj://img1", "size": 4096},
            {"content_type": "text", "inline_data": "Follow-up"},
        ]
        result = service.accept(_envelope(content_parts=parts))

        msg_events = EventStore(conn).read_stream("message", result.message_id)
        recorded = [e for e in msg_events if e.event_type == "interaction.message.recorded"]
        assert len(recorded) == 1
        descriptors = recorded[0].attributes.get("part_descriptors", [])
        content_types = {d.get("content_type") for d in descriptors}
        assert "text" in content_types
        assert "image" in content_types

    def test_empty_content_parts(self, service: InboundService, conn: sqlite3.Connection):
        """Empty content is allowed (some messages have no text)."""
        result = service.accept(_envelope(content_parts=[]))

        msg_events = EventStore(conn).read_stream("message", result.message_id)
        recorded = [e for e in msg_events if e.event_type == "interaction.message.recorded"]
        assert len(recorded) == 1
        descriptors = recorded[0].attributes.get("part_descriptors", [])
        assert len(descriptors) == 0


# =============================================================================
# Test Case 6: Event 与业务事务共同回滚
# =============================================================================


class TestRollback:
    def test_rollback_removes_all_data(self, tmp_path):
        """Simulate a crash by rolling back after intercepting the commit."""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod
        from cogito.infrastructure.payload_store import PayloadStore

        conn = _setup_in_memory_db()
        conn.execute("BEGIN")
        payload_store = PayloadStore(tmp_path / "payloads", conn)

        with (
            patch.object(uow_mod.UnitOfWork, "commit"),
            patch.object(uow_mod.UnitOfWork, "__exit__", return_value=None),
        ):
            svc = InboundService(conn, payload_store=payload_store)
            svc.accept(_envelope(platform_message_id="rollback_test"))

        conn.rollback()

        assert len(EventStore(conn).list_events(limit=10)) == 0
        conn.close()

    def test_rollback_allows_retry(self, tmp_path):
        """After rollback, same message can be re-processed successfully."""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod
        from cogito.infrastructure.payload_store import PayloadStore

        conn = _setup_in_memory_db()
        conn.execute("BEGIN")
        payload_store = PayloadStore(tmp_path / "payloads", conn)

        with (
            patch.object(uow_mod.UnitOfWork, "commit"),
            patch.object(uow_mod.UnitOfWork, "__exit__", return_value=None),
        ):
            svc1 = InboundService(conn, payload_store=payload_store)
            r1 = svc1.accept(_envelope(platform_message_id="retry_msg"))

        conn.rollback()

        svc2 = InboundService(conn, payload_store=payload_store)
        r2 = svc2.accept(_envelope(platform_message_id="retry_msg"))
        assert r2.is_new is True
        assert r2.message_id != r1.message_id
        conn.close()

    def test_partial_failure_no_partial_data(self, tmp_path):
        """When UoW exits without commit (e.g. exception), data is rolled back."""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod
        from cogito.infrastructure.payload_store import PayloadStore

        conn = _setup_in_memory_db()
        conn.execute("BEGIN")
        payload_store = PayloadStore(tmp_path / "payloads", conn)

        with (
            patch.object(uow_mod.UnitOfWork, "commit"),
            patch.object(uow_mod.UnitOfWork, "__exit__", return_value=None),
        ):
            svc = InboundService(conn, payload_store=payload_store)
            svc.accept(_envelope(platform_message_id="partial"))

        conn.rollback()

        assert len(EventStore(conn).list_events(limit=10)) == 0
        conn.close()


# =============================================================================
# Test Case 7: 两个并发重复请求最多创建一个逻辑 Turn
# =============================================================================


class TestConcurrentDedup:
    def test_concurrent_duplicate_creates_one_turn(self, tmp_path):
        """Two connections processing the same event concurrently produce one Turn."""
        import os
        import tempfile

        from cogito.infrastructure.payload_store import PayloadStore

        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)

        def _make_conn() -> sqlite3.Connection:
            from cogito.store.migration import migrate

            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.row_factory = sqlite3.Row
            migrate(conn)
            return conn

        results: list = []
        errors: list = []

        def _accept() -> None:
            try:
                conn = _make_conn()
                svc = InboundService(conn, payload_store=PayloadStore(tmp_path / "payloads", conn))
                r = svc.accept(
                    _envelope(
                        channel_instance_id="ci_conc",
                        platform_message_id="pm_conc",
                    )
                )
                results.append(r)
                conn.close()
            except Exception as e:
                errors.append(e)

        threads = [Thread(target=_accept) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Cleanup
        try:
            os.unlink(db_path)
        except OSError:
            pass

        # At most one of the two should succeed (the other gets deduped by Event)
        assert len(results) >= 1, errors
        turn_ids = {r.turn_id for r in results if r is not None}
        assert len(turn_ids) == 1, f"Expected 1 turn, got: {turn_ids}"


# =============================================================================
# Test Case 8: 不受信任内容只作为数据持久化，不改变策略字段
# =============================================================================


class TestUntrustedContent:
    def test_untrusted_saved_as_data(self, service: InboundService, conn: sqlite3.Connection):
        """Untrusted content is saved, but policy fields remain default."""
        result = service.accept(
            _envelope(
                trust_label="unverified",
                content_parts=[{"content_type": "text", "inline_data": "Suspicious content"}],
            )
        )

        # Verify via Event
        msg_events = EventStore(conn).read_stream("message", result.message_id)
        recorded = [e for e in msg_events if e.event_type == "interaction.message.recorded"]
        assert len(recorded) == 1
        assert recorded[0].attributes.get("trust_label") == "unverified"

        from cogito.store.event_replay import replay_turn

        turn = replay_turn(EventStore(conn).read_stream("turn", result.turn_id), result.turn_id)
        assert turn is not None
        assert turn.status == "queued"
        assert turn.priority == 80
