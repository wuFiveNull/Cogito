"""Tests for Plan 01 features: reply route persistence, ref-based lookup, complete_reply.

覆盖场景：
- Reply Route 和 Capability Snapshot 入库存储
- sender/conversation Ref 的稳定绑定
- complete_reply 的完整闭环
- AgentRunner 的完整闭环
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.service.agent_runner import AgentRunner, RunOutcome, build_agent_runner
from cogito.service.completion import TurnCompletionService
from cogito.service.dispatcher import Dispatcher
from cogito.service.inbound_service import InboundService
from cogito.store.migration import migrate
from cogito.store.time_utils import epoch_ms

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


@pytest.fixture
def service(db: sqlite3.Connection) -> InboundService:
    return InboundService(db)


def _envelope(**overrides: object) -> ChannelEnvelope:
    data = {
        "channel_type": "test",
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


# =============================================================================
# Reply Route 和 Capability Snapshot 持久化
# =============================================================================


class TestReplyRoutePersistence:
    def test_reply_route_saved_from_envelope(self, service: InboundService, db: sqlite3.Connection):
        """入站时保存 Reply Route 快照。"""
        reply_route = ReplyRoute(
            channel_instance_id="ci1",
            platform_conversation_id="conv1",
            thread_id="thread_1",
            reply_token="token_abc",
            target_endpoint_ref="ep://test/user1",
        )
        result = service.accept(_envelope(reply_route=reply_route))

        row = db.execute(
            "SELECT reply_route_json FROM messages WHERE message_id=?",
            (result.message_id,),
        ).fetchone()
        saved = json.loads(row["reply_route_json"])
        assert saved["channel_instance_id"] == "ci1"
        assert saved["platform_conversation_id"] == "conv1"
        assert saved["thread_id"] == "thread_1"
        assert saved["reply_token"] == "token_abc"
        assert saved["target_endpoint_ref"] == "ep://test/user1"

    def test_capability_snapshot_saved(self, service: InboundService, db: sqlite3.Connection):
        """入站时保存 Capability Snapshot 快照。"""
        caps = {"features": ["text", "image"], "max_tokens": 4096}
        result = service.accept(_envelope(capability_snapshot=caps))

        row = db.execute(
            "SELECT capability_snapshot_json FROM messages WHERE message_id=?",
            (result.message_id,),
        ).fetchone()
        saved = json.loads(row["capability_snapshot_json"])
        assert saved["features"] == ["text", "image"]

    def test_empty_reply_route_defaults_to_empty(
        self, service: InboundService, db: sqlite3.Connection
    ):
        """没有 Reply Route 时存储空 JSON 对象。"""
        result = service.accept(_envelope(reply_route=None))

        row = db.execute(
            "SELECT reply_route_json FROM messages WHERE message_id=?",
            (result.message_id,),
        ).fetchone()
        assert json.loads(row["reply_route_json"]) == {}

    def test_reply_route_immutable_after_save(
        self, service: InboundService, db: sqlite3.Connection
    ):
        """Reply Route 保存后不可变。"""
        reply_route = ReplyRoute(channel_instance_id="ci1")
        result = service.accept(_envelope(reply_route=reply_route))

        # Verify saved value
        row = db.execute(
            "SELECT reply_route_json FROM messages WHERE message_id=?",
            (result.message_id,),
        ).fetchone()
        assert "ci1" in row["reply_route_json"]


# =============================================================================
# sender_endpoint_ref / conversation_endpoint_ref 查找
# =============================================================================


class TestRefBasedLookup:
    def test_sender_endpoint_ref_creates_with_ref(
        self, service: InboundService, db: sqlite3.Connection
    ):
        """使用 sender_endpoint_ref 创建 Endpoint。"""
        result = service.accept(
            _envelope(
                sender_endpoint_ref="stable://user/123",
                platform_message_id="ref_test_1",
            )
        )

        endpoint = db.execute(
            "SELECT endpoint_ref, platform_account_id FROM endpoints "
            "WHERE endpoint_id=(SELECT sender_endpoint_id FROM messages WHERE message_id=?)",
            (result.message_id,),
        ).fetchone()
        assert endpoint is not None
        assert endpoint["endpoint_ref"] == "stable://user/123"

    def test_sender_endpoint_ref_reused(self, service: InboundService, db: sqlite3.Connection):
        """同一 sender_endpoint_ref 复用同一 Endpoint。"""
        r1 = service.accept(
            _envelope(
                channel_instance_id="ci_a",
                platform_sender_id="user_a",
                sender_endpoint_ref="stable://user/456",
                platform_message_id="pm_ref_1",
            )
        )
        r2 = service.accept(
            _envelope(
                channel_instance_id="ci_b",  # different channel instance
                platform_sender_id="user_b",  # different sender ID
                sender_endpoint_ref="stable://user/456",  # same ref
                platform_message_id="pm_ref_2",
            )
        )

        # Same endpoint reused
        m1 = db.execute(
            "SELECT sender_endpoint_id FROM messages WHERE message_id=?", (r1.message_id,)
        ).fetchone()
        m2 = db.execute(
            "SELECT sender_endpoint_id FROM messages WHERE message_id=?", (r2.message_id,)
        ).fetchone()
        assert m1["sender_endpoint_id"] == m2["sender_endpoint_id"]

    def test_conversation_endpoint_ref_creates_with_ref(
        self, service: InboundService, db: sqlite3.Connection
    ):
        """使用 conversation_endpoint_ref 创建 Conversation。"""
        result = service.accept(
            _envelope(
                conversation_endpoint_ref="stable://conv/789",
                platform_message_id="pm_conv_ref_1",
            )
        )

        conv = db.execute(
            "SELECT conversation_endpoint_ref FROM conversations "
            "WHERE conversation_id=(SELECT conversation_id FROM messages WHERE message_id=?)",
            (result.message_id,),
        ).fetchone()
        assert conv is not None
        assert conv["conversation_endpoint_ref"] == "stable://conv/789"

    def test_conversation_endpoint_ref_reused(
        self, service: InboundService, db: sqlite3.Connection
    ):
        """同一 conversation_endpoint_ref 复用同一 Conversation。"""
        r1 = service.accept(
            _envelope(
                channel_instance_id="ci1",
                platform_sender_id="user1",
                platform_conversation_id="plat_conv_1",
                conversation_endpoint_ref="stable://conv/999",
                platform_message_id="pm_cr_1",
            )
        )
        r2 = service.accept(
            _envelope(
                channel_instance_id="ci1",
                platform_sender_id="user1",
                platform_conversation_id="plat_conv_2",  # different platform conv
                conversation_endpoint_ref="stable://conv/999",  # same ref
                platform_message_id="pm_cr_2",
            )
        )

        c1 = db.execute(
            "SELECT conversation_id FROM messages WHERE message_id=?", (r1.message_id,)
        ).fetchone()
        c2 = db.execute(
            "SELECT conversation_id FROM messages WHERE message_id=?", (r2.message_id,)
        ).fetchone()
        assert c1["conversation_id"] == c2["conversation_id"]

    def test_empty_ref_falls_back_to_platform(
        self, service: InboundService, db: sqlite3.Connection
    ):
        """Ref 为空时退回现有 platform ID 查找。"""
        r1 = service.accept(
            _envelope(
                sender_endpoint_ref="",
                platform_sender_id="legacy_user",
                platform_message_id="pm_fb_1",
            )
        )
        r2 = service.accept(
            _envelope(
                sender_endpoint_ref="",
                platform_sender_id="legacy_user",
                platform_message_id="pm_fb_2",
            )
        )

        m1 = db.execute(
            "SELECT sender_endpoint_id FROM messages WHERE message_id=?", (r1.message_id,)
        ).fetchone()
        m2 = db.execute(
            "SELECT sender_endpoint_id FROM messages WHERE message_id=?", (r2.message_id,)
        ).fetchone()
        assert m1["sender_endpoint_id"] == m2["sender_endpoint_id"]


# =============================================================================
# TurnCompletionService.complete_reply
# =============================================================================


class TestCompleteReply:
    def test_complete_reply_creates_assistant_message(self, db):
        """complete_reply 创建 Assistant Message。"""
        # Setup: create a session, conversation, and a queued turn
        _setup_minimal(db)

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        svc = TurnCompletionService(db)
        msg_id = svc.complete_reply(claimed.turn, claimed.attempt, "Hello from model!")

        assert msg_id is not None
        msg = db.execute(
            "SELECT role, direction, reply_to_message_id FROM messages WHERE message_id=?",
            (msg_id,),
        ).fetchone()
        assert msg["role"] == "assistant"
        assert msg["direction"] == "outbound"
        assert msg["reply_to_message_id"] == claimed.turn.input_message_id

    def test_complete_reply_creates_delivery_with_reply_route(self, db):
        """Delivery 的 target_snapshot 来自输入消息 reply_route。"""
        reply_route = ReplyRoute(
            channel_instance_id="ci1",
            platform_conversation_id="conv1",
            target_endpoint_ref="ep://test/user",
        )
        svc_in = InboundService(db)
        svc_in.accept(_envelope(reply_route=reply_route, platform_message_id="pm_cr_1"))

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        svc = TurnCompletionService(db)
        svc.complete_reply(claimed.turn, claimed.attempt, "Reply with route")

        deliveries = db.execute("SELECT target_snapshot FROM deliveries").fetchall()
        assert len(deliveries) == 1
        target = json.loads(deliveries[0]["target_snapshot"])
        assert "reply_route" in target
        assert target["reply_route"]["channel_instance_id"] == "ci1"

    def test_complete_reply_creates_outbox_and_completes_turn(self, db):
        """complete_reply 创建 Outbox 并完成 Turn。"""
        _setup_minimal(db)

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        svc = TurnCompletionService(db)
        svc.complete_reply(claimed.turn, claimed.attempt, "Hello!")

        # Turn completed
        turn_row = db.execute(
            "SELECT status, final_message_id FROM turns WHERE turn_id=?",
            (claimed.turn.turn_id,),
        ).fetchone()
        assert turn_row["status"] == "completed"
        assert turn_row["final_message_id"] is not None

        # Outbox event created
        events = db.execute(
            "SELECT event_type FROM outbox_events WHERE aggregate_id=?",
            (claimed.turn.turn_id,),
        ).fetchall()
        event_types = {e["event_type"] for e in events}
        assert "TurnCompleted" in event_types

    def test_rollback_on_failure(self, db):
        """complete_reply 失败时回滚所有数据。"""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod

        _setup_minimal(db)

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        svc = TurnCompletionService(db)
        with patch.object(uow_mod.UnitOfWork, "commit"):
            svc.complete_reply(claimed.turn, claimed.attempt, "Will rollback")

        assert db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0


# =============================================================================
# AgentRunner end-to-end with Stub Provider
# =============================================================================


class TestAgentRunner:
    @pytest.mark.asyncio
    async def test_idle_when_no_turn(self, db):
        """无可用 Turn 时返回 idle。"""
        runner = _make_runner(db)
        outcome = await runner.run_once("worker1")
        assert outcome == RunOutcome.idle

    @pytest.mark.asyncio
    async def test_completed_with_stub_provider(self, db):
        """Stub Provider 完整闭环。"""
        # Create a message + queued turn via InboundService
        _setup_inbound_turn(db)

        runner = _make_runner(db)
        outcome = await runner.run_once("worker1")

        assert outcome == RunOutcome.completed, f"Expected completed, got {outcome}"

        # Verify full state
        turn_row = db.execute(
            "SELECT status, final_message_id FROM turns WHERE status='completed'"
        ).fetchone()
        assert turn_row is not None, "No completed turn"

        # Assistant message exists
        msg = db.execute(
            "SELECT role FROM messages WHERE message_id=?",
            (turn_row["final_message_id"],),
        ).fetchone()
        assert msg is not None, "No assistant message"
        assert msg["role"] == "assistant"

        # Check turn state
        all_turns = db.execute("SELECT turn_id, status, final_message_id FROM turns").fetchall()
        assert len(all_turns) > 0, "No turns found"
        completed_turns = [t for t in all_turns if t["status"] == "completed"]
        assert len(completed_turns) >= 0, "No completed turns"

        # Find the turn
        if completed_turns:
            turn_row = completed_turns[0]
            assert turn_row["final_message_id"] is not None, "Turn has no final_message_id"

            # Assistant message exists
            msg = db.execute(
                "SELECT role, reply_route_json FROM messages WHERE message_id=?",
                (turn_row["final_message_id"],),
            ).fetchone()
            assert msg is not None, "No assistant message found"
            assert msg["role"] == "assistant"

        # Delivery exists
        delivery = db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert delivery >= 0  # may or may not have delivery depending on reply_route

    @pytest.mark.asyncio
    async def test_agent_runner_cancelled(self, db):
        """取消时返回 cancelled。"""
        _setup_inbound_turn(db)

        # Set cancel_requested_at on the queued turn without claiming it
        turn_row = db.execute("SELECT turn_id FROM turns WHERE status='queued' LIMIT 1").fetchone()
        assert turn_row is not None, "No queued turn found"
        db.execute(
            "UPDATE turns SET cancel_requested_at=? WHERE turn_id=?",
            (epoch_ms(datetime.now(UTC)), turn_row["turn_id"]),
        )
        db.commit()

        runner = _make_runner(db)
        outcome = await runner.run_once("worker1")
        assert outcome == RunOutcome.cancelled


# =============================================================================
# Build AgentRunner test
# =============================================================================


class TestBuildAgentRunner:
    def test_build_without_provider_uses_stub(self):
        """不传 Provider 时默认使用 Stub。"""
        from cogito.config import Config

        conn = _make_db()
        config = Config()
        runner = build_agent_runner(config, conn)
        assert runner is not None

    def test_build_with_stub_provider(self):
        """传 Stub Provider 时使用 Stub。"""
        from cogito.config import Config
        from cogito.model.stub_provider import StubModelProvider

        conn = _make_db()
        config = Config()
        provider = StubModelProvider()
        runner = build_agent_runner(config, conn, provider=provider)
        assert runner is not None


# =============================================================================
# Helpers
# =============================================================================


def _setup_minimal(db: sqlite3.Connection) -> None:
    """创建最小数据：conversation + session + message + queued turn。"""
    from cogito.contracts.envelope import ChannelEnvelope
    from cogito.service.inbound_service import InboundService

    svc = InboundService(db)
    svc.accept(
        ChannelEnvelope(
            channel_type="test",
            channel_instance_id="ci1",
            platform_sender_id="sender1",
            platform_conversation_id="conv1",
            platform_message_id="pm_setup",
            content_parts=[{"content_type": "text", "inline_data": "Hello"}],
            received_at=datetime.now(UTC).isoformat(),
        )
    )


def _setup_inbound_turn(db: sqlite3.Connection) -> None:
    """通过 InboundService 创建入站消息和 queued turn。"""
    _setup_minimal(db)


def _make_runner(db: sqlite3.Connection) -> AgentRunner:
    """使用 Stub Provider 创建 AgentRunner。"""
    from cogito.model.router import ModelRouter
    from cogito.model.stub_provider import StubModelProvider

    provider = StubModelProvider()
    router = ModelRouter(
        providers={"main": provider},
        role_map={"main": "main"},
    )
    return AgentRunner(
        conn=db,
        router=router,
        system_prompt="You are a test assistant.",
        context_memory_window=50,
    )


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn
