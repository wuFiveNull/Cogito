"""Tests for accept_inbound application service (P2 core transaction)."""

import sqlite3
from datetime import UTC, datetime
from threading import Thread

import pytest

from cogito.contracts.envelope import ChannelEnvelope
from cogito.service.inbound_service import InboundService

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
def service(in_memory_db: sqlite3.Connection) -> InboundService:
    return InboundService(in_memory_db)


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

        # Message exists
        msg = conn.execute(
            "SELECT message_id, role, direction, receive_sequence FROM messages WHERE message_id=?",
            (result.message_id,),
        ).fetchone()
        assert msg is not None
        assert msg["role"] == "user"
        assert msg["direction"] == "inbound"
        assert msg["receive_sequence"] == 1

        # Turn exists and is queued
        turn = conn.execute(
            "SELECT turn_id, status, version, input_message_id FROM turns WHERE turn_id=?",
            (result.turn_id,),
        ).fetchone()
        assert turn is not None
        assert turn["status"] == "queued"
        assert turn["version"] == 2  # accepted(1) → queued(2)
        assert turn["input_message_id"] == result.message_id

    def test_creates_content_parts(self, service: InboundService, conn: sqlite3.Connection):
        result = service.accept(
            _envelope(
                content_parts=[
                    {"content_type": "text", "inline_data": "Part 1"},
                    {"content_type": "text", "inline_data": "Part 2"},
                ],
            )
        )

        parts = conn.execute(
            "SELECT content_type, inline_data FROM content_parts WHERE message_id=?",
            (result.message_id,),
        ).fetchall()
        assert len(parts) == 2
        inline_data = {r["inline_data"] for r in parts}
        assert "Part 1" in inline_data
        assert "Part 2" in inline_data

    def test_creates_inbox_record(self, service: InboundService, conn: sqlite3.Connection):
        result = service.accept(_envelope(channel_instance_id="ci1", platform_message_id="pm1"))

        inbox = conn.execute(
            "SELECT status, message_id FROM inbound_inbox WHERE channel_instance_id='ci1' AND platform_event_id='pm1'"
        ).fetchone()
        assert inbox is not None
        assert inbox["status"] == "processed"
        assert inbox["message_id"] == result.message_id

    def test_creates_outbox_events(self, service: InboundService, conn: sqlite3.Connection):
        result = service.accept(_envelope())

        events = conn.execute(
            "SELECT event_type, aggregate_type, aggregate_id FROM outbox_events ORDER BY created_at"
        ).fetchall()
        assert len(events) == 2
        assert events[0]["event_type"] == "InboundMessageAccepted"
        assert events[0]["aggregate_id"] == result.message_id
        assert events[1]["event_type"] == "TurnQueued"
        assert events[1]["aggregate_id"] == result.turn_id

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

        principal = conn.execute("SELECT principal_type, status FROM principals").fetchone()
        assert principal is not None
        assert principal["principal_type"] == "external_user"

        endpoint = conn.execute(
            "SELECT channel_type, platform_account_id, principal_id FROM endpoints"
        ).fetchone()
        assert endpoint is not None
        assert endpoint["channel_type"] == "tg"
        assert endpoint["platform_account_id"] == "user_a"

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

        conv = conn.execute(
            "SELECT conversation_type, platform_conversation_id FROM conversations"
        ).fetchone()
        assert conv is not None
        assert conv["conversation_type"] == "private"
        assert conv["platform_conversation_id"] == "gc1"

        session = conn.execute("SELECT status FROM sessions").fetchone()
        assert session is not None
        assert session["status"] == "active"


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

    def test_outbox_not_duplicated(self, service: InboundService, conn: sqlite3.Connection):
        service.accept(_envelope())
        count1 = _count_rows(conn, "outbox_events")

        service.accept(_envelope())
        count2 = _count_rows(conn, "outbox_events")

        assert count2 == count1  # no extra outbox events


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

        convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        assert convs == 1  # one conversation reused

    def test_sequence_monotonic(self, service: InboundService, conn: sqlite3.Connection):
        service.accept(_envelope(platform_message_id="pm1"))
        service.accept(_envelope(platform_message_id="pm2"))
        service.accept(_envelope(platform_message_id="pm3"))

        seqs = [
            r["receive_sequence"]
            for r in conn.execute(
                "SELECT receive_sequence FROM messages ORDER BY receive_sequence"
            ).fetchall()
        ]
        assert seqs == [1, 2, 3]

    def test_reuses_session(self, service: InboundService, conn: sqlite3.Connection):
        service.accept(_envelope(platform_message_id="pm1"))
        service.accept(_envelope(platform_message_id="pm2"))

        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert sessions == 1  # one session reused


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

        rows = conn.execute(
            "SELECT content_type, inline_data, payload_ref, size "
            "FROM content_parts WHERE message_id=?",
            (result.message_id,),
        ).fetchall()
        assert len(rows) == 3
        content_types = {r["content_type"] for r in rows}
        assert "text" in content_types
        assert "image" in content_types
        # Verify the image part has the expected payload
        img = [r for r in rows if r["content_type"] == "image"][0]
        assert img["payload_ref"] == "obj://img1"
        assert img["size"] == 4096

    def test_empty_content_parts(self, service: InboundService, conn: sqlite3.Connection):
        """Empty content is allowed (some messages have no text)."""
        result = service.accept(_envelope(content_parts=[]))

        count = conn.execute(
            "SELECT COUNT(*) FROM content_parts WHERE message_id=?",
            (result.message_id,),
        ).fetchone()[0]
        assert count == 0


# =============================================================================
# Test Case 6: Outbox 与业务事务共同回滚
# =============================================================================


class TestRollback:
    def test_rollback_removes_all_data(self):
        """Simulate a crash by rolling back after intercepting the commit."""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod

        conn = _setup_in_memory_db()
        conn.execute("BEGIN")

        with (
            patch.object(uow_mod.UnitOfWork, "commit"),
            patch.object(uow_mod.UnitOfWork, "__exit__", return_value=None),
        ):
            svc = InboundService(conn)
            svc.accept(_envelope(platform_message_id="rollback_test"))

        conn.rollback()

        assert _count_rows(conn, "messages") == 0
        assert _count_rows(conn, "turns") == 0
        assert _count_rows(conn, "outbox_events") == 0
        assert _count_rows(conn, "inbound_inbox") == 0
        conn.close()

    def test_rollback_allows_retry(self):
        """After rollback, same message can be re-processed successfully."""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod

        conn = _setup_in_memory_db()
        conn.execute("BEGIN")

        with (
            patch.object(uow_mod.UnitOfWork, "commit"),
            patch.object(uow_mod.UnitOfWork, "__exit__", return_value=None),
        ):
            svc1 = InboundService(conn)
            r1 = svc1.accept(_envelope(platform_message_id="retry_msg"))

        conn.rollback()

        # Retry after rollback - should succeed as a new message
        svc2 = InboundService(conn)
        r2 = svc2.accept(_envelope(platform_message_id="retry_msg"))
        assert r2.is_new is True
        assert r2.message_id != r1.message_id
        conn.close()

    def test_partial_failure_no_partial_data(self):
        """When UoW exits without commit (e.g. exception), data is rolled back."""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod

        conn = _setup_in_memory_db()
        conn.execute("BEGIN")

        with (
            patch.object(uow_mod.UnitOfWork, "commit"),
            patch.object(uow_mod.UnitOfWork, "__exit__", return_value=None),
        ):
            svc = InboundService(conn)
            svc.accept(_envelope(platform_message_id="partial"))

        conn.rollback()

        assert _count_rows(conn, "messages") == 0
        assert _count_rows(conn, "turns") == 0
        conn.close()


# =============================================================================
# Test Case 7: 两个并发重复请求最多创建一个逻辑 Turn
# =============================================================================


class TestConcurrentDedup:
    def test_concurrent_duplicate_creates_one_turn(self):
        """Two connections processing the same event concurrently produce one Turn."""
        import os
        import tempfile

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
                svc = InboundService(conn)
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

        # At most one of the two should succeed (the other gets an IntegrityError
        # from the UNIQUE constraint on inbound_inbox)
        assert len(results) >= 1
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

        msg = conn.execute(
            "SELECT trust_label FROM messages WHERE message_id=?",
            (result.message_id,),
        ).fetchone()
        assert msg["trust_label"] == "unverified"

        turn = conn.execute(
            "SELECT status, priority FROM turns WHERE turn_id=?",
            (result.turn_id,),
        ).fetchone()
        assert turn["status"] == "queued"  # still queued normally
        assert turn["priority"] == 80  # default priority, not changed by content
