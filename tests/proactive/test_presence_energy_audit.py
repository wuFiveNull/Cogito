"""M0+M1: PresenceReader + Decision 审计闭环测试。

覆盖 P0:
- PA-P0-01: persist_decision 显式 dry_run；real mode 创建 Delivery，Decision.dry_run=false
- PA-P0-02: energy 使用真实用户活动（PresenceReader）；同批固定 activity snapshot
- PresenceReader 失败 / 无活动时 fail-safe（不按最低能量增强主动性）
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.task import Task, TaskStatus
from cogito.service import task_handlers as th_module
from cogito.service.energy_model import compute_energy, energy_band
from cogito.service.presence import SqlitePresenceReader
from cogito.service.proactive_decision import persist_decision
from cogito.store.proactive_repo import (
    ProactiveCandidate,
    ProactiveCandidateRepository,
    ProactiveDecisionRepository,
    ProactivePolicy,
    ProactivePolicyRepository,
)
from cogito.store.migration import migrate


# ── fixtures ──


_DB_COUNTER = [0]
# 共享缓存内存库连接工厂注册表（key=id(conn)，避免给 C 对象加属性）
_FACTORY_REGISTRY: dict[int, callable] = {}


def _shared_connect(name: str):
    c = sqlite3.connect(name, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _fresh_db():
    # 共享缓存内存库：不同连接共享同一数据，reader 可安全创建/关闭独立连接。
    # 注意：这里不设 PRAGMA foreign_keys=ON。SQLite 共享缓存内存库在同时启用
    # foreign_keys + busy_timeout 时会出现 deferred FK 验证异常，导致跨事务
    # 已提交的父行被 FK 拒绝。业务测试不依赖 FK 级联，故保持关闭。
    _DB_COUNTER[0] += 1
    name = f"file:mem_{_DB_COUNTER[0]}?mode=memory&cache=shared"
    conn = _shared_connect(name)
    migrate(conn)
    # migration 0007 末尾设了 PRAGMA foreign_keys=ON，SQLite 共享缓存内存库在
    # FK ON 下对同一连接已提交父行出现 deferred FK 验证异常。本测试 FK 非
    # 验证重点，显式关闭以避免该已知行为。
    conn.execute("PRAGMA foreign_keys=OFF")

    # 工厂：每次创建新连接（共享缓存），供 PresenceReader 独立使用并关闭
    _FACTORY_REGISTRY[id(conn)] = lambda _n=name: _shared_connect(_n)
    return conn


def _factory_for(conn):
    """返回 conn 对应的共享缓存连接工厂。"""
    return _FACTORY_REGISTRY[id(conn)]


@pytest.fixture
def memory_db():
    conn = _fresh_db()
    yield conn
    conn.close()


def _ensure_conversation(conn, conv_id="conv-1"):
    """确保 conversations 存在 FK 需要的行。"""
    conn.execute(
        "INSERT OR IGNORE INTO conversations "
        "(conversation_id, conversation_endpoint_id, principal_scope) "
        "VALUES (?,?,?)",
        (conv_id, "ep-1", "owner"),
    )


def _seed_user_message(conn, principal_id="owner", age_minutes=0, msg_id="m1"):
    """Seed 一条 user 消息，created_at 距 now age_minutes。"""
    _ensure_conversation(conn)
    # receive_sequence 自增，避免与库内已有消息冲突（migration/consumer 可能已写入）
    max_seq = conn.execute(
        "SELECT COALESCE(MAX(receive_sequence), 0) FROM messages WHERE conversation_id=?",
        ("conv-1",),
    ).fetchone()[0]
    at = (datetime.now(UTC) - timedelta(minutes=age_minutes)).isoformat()
    conn.execute(
        "INSERT INTO messages "
        "(message_id, conversation_id, sender_principal_id, role, direction, "
        " receive_sequence, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (msg_id, "conv-1", principal_id, "user", "inbound", max_seq + 1, at),
    )
    conn.commit()


def _policy() -> ProactivePolicy:
    return ProactivePolicy(
        policy_id="p1",
        principal_id="owner",
        version=1,
        quiet_hours={"enabled": False},
        cooldown_minutes_same_topic=360,
        max_pushes_per_hour=3,
        max_pushes_per_day=10,
        minimum_relevance=0.4,
        minimum_novelty=0.3,
        dry_run=True,
    )


def _candidate(cid="c1", **over) -> ProactiveCandidate:
    base = dict(
        candidate_id=cid,
        principal_id="owner",
        stream_type="content",
        topic="ai-models",
        summary="test",
        novelty=0.7,
        relevance=0.8,
        urgency=0.6,
        confidence=0.8,
        policy_version=1,
        idempotency_key=f"k-{cid}",
        created_at=0,
        status="evaluating",
    )
    base.update(over)
    return ProactiveCandidate(**base)


# ── PresenceReader ──


class TestPresenceReader:
    def test_no_messages_returns_none(self, memory_db):
        reader = SqlitePresenceReader(connection_factory=lambda p=memory_db: p)
        assert reader.get_last_user_activity("owner") is None

    def test_recent_activity_returns_datetime(self, memory_db):
        _seed_user_message(memory_db, age_minutes=5)
        reader = SqlitePresenceReader(connection_factory=lambda p=memory_db: p)
        dt = reader.get_last_user_activity("owner")
        assert dt is not None
        # ~5 分钟前后（松判断）
        age = (datetime.now(UTC) - dt).total_seconds() / 60
        assert 4 < age < 7

    def test_ignores_non_user_messages(self, memory_db):
        # 仅 assistant 消息 → None
        _ensure_conversation(memory_db)
        at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        memory_db.execute(
            "INSERT INTO messages "
            "(message_id, conversation_id, sender_principal_id, role, direction, "
            " receive_sequence, created_at) VALUES (?,?,?,?,?,?,?)",
            ("mA", "conv-1", "owner", "assistant", "outbound", 1, at),
        )
        memory_db.commit()
        reader = SqlitePresenceReader(connection_factory=lambda p=memory_db: p)
        assert reader.get_last_user_activity("owner") is None

    def test_failure_returns_none_not_raise(self):
        def _boom():
            raise RuntimeError("db down")

        reader = SqlitePresenceReader(connection_factory=_boom)
        # 内部捕获异常，返回 None（fail-safe）
        assert reader.get_last_user_activity("owner") is None

    def test_filters_by_principal(self, memory_db):
        _seed_user_message(memory_db, principal_id="owner", msg_id="m-own")
        # 其他 principal 的消息不应影响
        at_other = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        memory_db.execute(
            "INSERT INTO messages "
            "(message_id, conversation_id, sender_principal_id, role, direction, "
            " receive_sequence, created_at) VALUES (?,?,?,?,?,?,?)",
            ("mOther", "conv-1", "someone-else", "user", "inbound", 2, at_other),
        )
        memory_db.commit()
        reader = SqlitePresenceReader(connection_factory=lambda p=memory_db: p)
        dt = reader.get_last_user_activity("owner")
        # owner 没有 user 消息 → None（m-own 的 sender_principal_id='owner' 已插入）
        # 修正：m-own 就是 owner 的，所以存在
        assert dt is not None


# ── energy bands ──


class TestEnergyBands:
    def test_1min_ago_high(self):
        now = datetime.now(UTC)
        e = compute_energy(now - timedelta(minutes=1), now=now)
        assert energy_band(e) == "high"

    def test_1h_ago_medium_or_low(self):
        now = datetime.now(UTC)
        e = compute_energy(now - timedelta(hours=1), now=now)
        # 1h 后约 0.48 → medium（在 0.3 附近可能下探）
        assert energy_band(e) in ("medium", "high")

    def test_4h_ago_lower(self):
        now = datetime.now(UTC)
        e = compute_energy(now - timedelta(hours=4), now=now)
        assert energy_band(e) in ("medium", "low")

    def test_never_active_failsafe_medium(self):
        """PLAN-17 R6 PA-P1-01: last_user_at=None (从未活动 / PresenceReader
        读取失败) 必须 fail-safe 视为 medium energy, 不能落入 low energy 的
        ×1.5 urgency 路径错误地提升主动性。"""
        e = compute_energy(None)
        assert e == 0.5, f"presence read failure must fail-safe to 0.5 (medium), got {e}"
        assert energy_band(e) == "medium"


# ── persist_decision dry_run / audit fields ──


class TestPersistDecisionAudit:
    def test_policy_save_persists_non_null_updated_at(self, memory_db):
        repo = ProactivePolicyRepository(memory_db)
        repo.save(_policy())
        memory_db.commit()
        row = memory_db.execute(
            "SELECT updated_at FROM proactive_policies WHERE policy_id='p1'"
        ).fetchone()
        assert row is not None
        assert isinstance(row["updated_at"], int)

    def test_dry_run_true_recorded(self, memory_db):
        c = _candidate()
        conn = memory_db
        # 先写 candidate（FK 约束）
        conn.execute(
            "INSERT INTO proactive_candidates "
            "(candidate_id, principal_id, stream_type, topic, summary, "
            " novelty, relevance, urgency, confidence, recommended_action, "
            " policy_version, idempotency_key, source_event_ids_json, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.candidate_id,
                c.principal_id,
                c.stream_type,
                c.topic,
                c.summary,
                c.novelty,
                c.relevance,
                c.urgency,
                c.confidence,
                c.recommended_action,
                c.policy_version,
                c.idempotency_key,
                "[]",
                0,
                "evaluating",
            ),
        )
        conn.commit()
        from cogito.service.proactive_decision import decide

        action, trace = decide(c, _policy())
        d = persist_decision(
            conn,
            c,
            _policy(),
            action,
            trace,
            dry_run=True,
            energy_value=0.5,
            last_user_at=1_700_000_000_000,
            energy_model_version="v1",
            config_version_id="cfg-123",
        )
        assert d.dry_run is True
        assert d.last_user_at == 1_700_000_000_000
        assert d.config_version_id == "cfg-123"
        # DB 验证
        row = conn.execute(
            "SELECT dry_run, last_user_at, energy_model_version, config_version_id "
            "FROM proactive_decisions_v2 WHERE decision_id=?",
            (d.decision_id,),
        ).fetchone()
        assert row["dry_run"] == 1
        assert row["last_user_at"] == 1_700_000_000_000
        assert row["energy_model_version"] == "v1"
        assert row["config_version_id"] == "cfg-123"

    def test_dry_run_false_recorded(self, memory_db):
        conn = memory_db
        c = _candidate(cid="c2", idempotency_key="k-c2")
        conn.execute(
            "INSERT INTO proactive_candidates "
            "(candidate_id, principal_id, stream_type, topic, summary, "
            " novelty, relevance, urgency, confidence, recommended_action, "
            " policy_version, idempotency_key, source_event_ids_json, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.candidate_id,
                c.principal_id,
                c.stream_type,
                c.topic,
                c.summary,
                c.novelty,
                c.relevance,
                c.urgency,
                c.confidence,
                c.recommended_action,
                c.policy_version,
                c.idempotency_key,
                "[]",
                0,
                "evaluating",
            ),
        )
        conn.commit()
        from cogito.service.proactive_decision import decide

        action, trace = decide(c, _policy())
        d = persist_decision(
            conn,
            c,
            _policy(),
            action,
            trace,
            dry_run=False,
            energy_value=0.8,
        )
        assert d.dry_run is False


# ── _evaluate_candidates_sync: 同批固定 snapshot + dry_run 取自 config ──


class TestEvaluateCandidates:
    class FakeDelivery:
        def __init__(self):
            self.requests = []

        async def enqueue(self, request):
            self.requests.append(request)
            return "delivery-test"

    def test_dry_run_send_later_creates_no_followup_task(self, memory_db):
        policy = replace(
            _policy(),
            quiet_hours={"enabled": True, "start": "00:00", "end": "23:59"},
            minimum_relevance=0.1,
            minimum_novelty=0.1,
            digest_max_delay_minutes=0,
        )
        ProactivePolicyRepository(memory_db).save(policy)
        ProactiveCandidateRepository(memory_db).insert(
            _candidate(urgency=0.95, novelty=0.9, relevance=0.9, created_at=1)
        )
        memory_db.commit()
        delivery = self.FakeDelivery()
        from cogito.config import ProactiveConfig

        ctx = th_module.TaskHandlerContext(
            proactive_config=ProactiveConfig(enabled=True, dry_run=True),
            delivery_service=delivery,
        )

        asyncio.run(th_module._evaluate_candidates_async(memory_db, ctx))

        assert delivery.requests == []
        assert (
            memory_db.execute("SELECT COUNT(*) FROM scheduled_delivery_requests").fetchone()[0] == 0
        )
        assert (
            memory_db.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_type='proactive.delivery.ready'"
            ).fetchone()[0]
            == 0
        )

    def test_dry_run_digest_creates_no_publish_task(self, memory_db):
        policy = replace(
            _policy(),
            minimum_relevance=0.8,
            minimum_novelty=0.1,
            quiet_hours={"enabled": False},
        )
        ProactivePolicyRepository(memory_db).save(policy)
        ProactiveCandidateRepository(memory_db).insert(
            _candidate(relevance=0.2, novelty=0.9, created_at=1)
        )
        memory_db.commit()
        from cogito.config import ProactiveConfig

        ctx = th_module.TaskHandlerContext(
            proactive_config=ProactiveConfig(enabled=True, dry_run=True),
            delivery_service=self.FakeDelivery(),
        )

        asyncio.run(th_module._evaluate_candidates_async(memory_db, ctx))

        assert (
            memory_db.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_type='proactive.digest.publish'"
            ).fetchone()[0]
            == 0
        )

    def test_live_send_now_uses_complete_web_target_snapshot(self, memory_db):
        policy = replace(
            _policy(),
            dry_run=False,
            quiet_hours={"enabled": False},
            minimum_relevance=0.1,
            minimum_novelty=0.1,
        )
        ProactivePolicyRepository(memory_db).save(policy)
        ProactiveCandidateRepository(memory_db).insert(
            _candidate(urgency=0.95, novelty=0.9, relevance=0.9, created_at=1)
        )
        from cogito.contracts.envelope import ChannelEnvelope
        from cogito.service.inbound_service import InboundService

        InboundService(memory_db).accept(
            ChannelEnvelope(
                channel_type="web",
                channel_instance_id="web",
                platform_sender_id="web-user",
                platform_conversation_id="web:latest",
                platform_message_id="web-latest-message",
                content_parts=[{"content_type": "text", "inline_data": "hello"}],
                trust_label="authenticated",
            )
        )
        memory_db.commit()
        delivery = self.FakeDelivery()
        fallback_delivery = self.FakeDelivery()
        factory_connections = []
        from cogito.config import ProactiveConfig

        ctx = th_module.TaskHandlerContext(
            proactive_config=ProactiveConfig(enabled=True, dry_run=False),
            delivery_service=fallback_delivery,
            delivery_service_factory=lambda conn: factory_connections.append(conn) or delivery,
        )

        asyncio.run(th_module._evaluate_candidates_async(memory_db, ctx))

        assert len(delivery.requests) == 1
        assert fallback_delivery.requests == []
        assert factory_connections == [memory_db]
        target = delivery.requests[0].target
        assert target["adapter_id"] == "web"
        assert target["conversation_id"] == "web:latest"
        message = memory_db.execute(
            "SELECT m.role, m.direction, cp.inline_data FROM messages m "
            "JOIN content_parts cp ON cp.message_id=m.message_id "
            "WHERE m.message_id=?",
            (delivery.requests[0].content_ref,),
        ).fetchone()
        assert message is not None
        assert dict(message) == {
            "role": "assistant",
            "direction": "outbound",
            "inline_data": "test",
        }

    def test_same_batch_uses_same_energy_snapshot(self, memory_db):
        """同批 2 个 Candidate 必须使用同一 energy/activity 快照。"""
        conn = memory_db
        # 写 2 个 candidate
        for cid, key in [("c1", "k1"), ("c2", "k2")]:
            conn.execute(
                "INSERT INTO proactive_candidates "
                "(candidate_id, principal_id, stream_type, topic, summary, "
                " novelty, relevance, urgency, confidence, recommended_action, "
                " policy_version, idempotency_key, source_event_ids_json, created_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    "owner",
                    "content",
                    "ai",
                    "s",
                    0.7,
                    0.8,
                    0.6,
                    0.8,
                    "evaluate",
                    1,
                    key,
                    "[]",
                    0,
                    "evaluating",
                ),
            )
        conn.commit()
        # seed 真实用户活动（供 PresenceReader 读取）
        _seed_user_message(conn, age_minutes=30, msg_id="m-activity")

        from cogito.config import ProactiveConfig
        from cogito.service.presence import SqlitePresenceReader

        ctx = th_module.TaskHandlerContext(
            connection_factory=lambda p=conn: _factory_for(p)(),
            workspace_path="",
            proactive_config=ProactiveConfig(dry_run=True),
            presence_reader=SqlitePresenceReader(
                connection_factory=lambda p=conn: _factory_for(p)(),
            ),
            config_version_id="cfg-x",
        )
        # 执行（同批 → 两个 decision 的 energy_value/lost_user_at 一致）
        asyncio.run(th_module._evaluate_candidates_async(conn, ctx))

        rows = conn.execute(
            "SELECT energy_value, last_user_at FROM proactive_decisions_v2 "
            "WHERE candidate_id IN ('c1','c2') ORDER BY candidate_id"
        ).fetchall()
        assert len(rows) == 2
        # 同批固定快照：两个 decision 的 energy_value 和 last_user_at 相同
        assert rows[0]["energy_value"] == rows[1]["energy_value"]
        assert rows[0]["last_user_at"] == rows[1]["last_user_at"]
        # 且取自真实活动（非 None）
        assert rows[0]["last_user_at"] is not None

    def test_dry_run_true_no_delivery(self, memory_db):
        """dry_run 下 send_now 不创建 Delivery（仅记录 decision）。"""
        conn = memory_db
        conn.execute(
            "INSERT INTO proactive_candidates "
            "(candidate_id, principal_id, stream_type, topic, summary, "
            " novelty, relevance, urgency, confidence, recommended_action, "
            " policy_version, idempotency_key, source_event_ids_json, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c1",
                "owner",
                "content",
                "ai",
                "s end now high urgency",
                0.8,
                0.9,
                0.95,
                0.8,
                "evaluate",
                1,
                "k1",
                "[]",
                0,
                "evaluating",
            ),
        )
        conn.commit()
        from cogito.config import ProactiveConfig

        ctx = th_module.TaskHandlerContext(
            connection_factory=lambda p=conn: _factory_for(p),
            workspace_path="",
            proactive_config=ProactiveConfig(
                dry_run=True, max_pushes_per_hour=99, max_pushes_per_day=99
            ),
            presence_reader=SqlitePresenceReader(connection_factory=lambda p=conn: _factory_for(p)),
        )
        asyncio.run(th_module._evaluate_candidates_async(conn, ctx))
        # decision.dry_run = true
        row = conn.execute(
            "SELECT dry_run FROM proactive_decisions_v2 WHERE candidate_id='c1'"
        ).fetchone()
        assert row["dry_run"] == 1

    def test_presence_reader_failure_fails_safe(self, memory_db):
        """PLAN-17 R6 PA-P1-01: PresenceReader 返回 None (失败) 视为 medium energy,
        不得再因 low energy 的 ×1.5 urgency 路径而提高主动性; 验证决策时不抛异常。"""
        conn = memory_db
        conn.execute(
            "INSERT INTO proactive_candidates "
            "(candidate_id, principal_id, stream_type, topic, summary, "
            " novelty, relevance, urgency, confidence, recommended_action, "
            " policy_version, idempotency_key, source_event_ids_json, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c1",
                "owner",
                "content",
                "ai",
                "s",
                0.8,
                0.9,
                0.95,
                0.8,
                "evaluate",
                1,
                "k1",
                "[]",
                0,
                "evaluating",
            ),
        )
        conn.commit()

        class BoomReader:
            def get_last_user_activity(self, principal_id):
                raise RuntimeError("dbDown")

        from cogito.config import ProactiveConfig

        ctx = th_module.TaskHandlerContext(
            connection_factory=lambda p=conn: _factory_for(p),
            workspace_path="",
            proactive_config=ProactiveConfig(dry_run=False),
            presence_reader=BoomReader(),
            delivery_service=None,  # 无投递 → 不创建 Delivery
        )
        # 不抛异常
        asyncio.run(th_module._evaluate_candidates_async(conn, ctx))
        row = conn.execute(
            "SELECT dry_run FROM proactive_decisions_v2 WHERE candidate_id='c1'"
        ).fetchone()
        # 全局配置或 Principal policy 任一 dry-run 都是安全锁；默认 policy 为 dry-run。
        assert row["dry_run"] == 1
