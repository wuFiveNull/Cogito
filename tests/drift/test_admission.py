"""M3: DriftAdmissionService 全局 idle admission 测试。

Admission 矩阵 (PROACTIVE-IDLE / 9):
active_turn, priority_backlog, delivery_backlog, outbox_critical,
recovery_in_progress, budget_exhausted, not_idle_long_enough,
drift_already_active → 各 deny；全满足 → admit。

并发唯一性：同 Principal/Profile 同时最多一个 active Drift。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from cogito.service.drift_admission import admit
from cogito.store.migration import migrate


# ── fixtures ──


_SEQ = [0]


def _uniq(tag: str) -> str:
    _SEQ[0] += 1
    return f"{tag}-{_SEQ[0]}-{int(time.time()*1000)%100000}"


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def memory_db():
    conn = _fresh_db()
    yield conn
    conn.close()


# ── helpers ──


def _seed_turn(conn, status="running"):
    tid = _uniq(f"turn-{status}")
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, input_message_id, status, priority, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (tid, "sess-1", "msg-1", status, 80,
         (datetime.now(UTC)).isoformat()),
    )
    conn.commit()
    return tid


def _seed_task(conn, priority=50, status="queued", task_type="knowledge.embed"):
    tid = _uniq("task")
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (tid, task_type, status, priority, f"idemp-{tid}",
         int(time.time()*1000)),
    )
    conn.commit()
    return tid


def _seed_drift_run(conn, run_id, task_id=None, status="completed"):
    """写入一条 drift_runs（需先有对应 task 因 FK task_id→tasks）。"""
    tid = task_id if task_id else _seed_task(conn, task_type="drift.run")
    conn.execute(
        "INSERT INTO drift_runs "
        "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
        " status, admission_snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, tid, "owner", "s", "1.0", status, "{}", int(time.time()*1000)),
    )
    conn.commit()
    return tid


def _seed_delivery(conn, status="pending"):
    did = _uniq("del")
    conn.execute(
        "INSERT INTO deliveries (delivery_id, status, idempotency_key, created_at) "
        "VALUES (?,?,?,?)",
        (did, status, f"idem-{did}", int(time.time()*1000)),
    )
    conn.commit()


def _seed_outbox(conn, age_ms=0):
    eid = _uniq("evt")
    created = int(time.time()*1000) - age_ms
    conn.execute(
        "INSERT INTO outbox_events "
        "(event_id, event_type, aggregate_type, aggregate_id, "
        " status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (eid, "TestEvent", "test", "agg-1", "pending", created),
    )
    conn.commit()


class _ReaderFactory:
    """通过 last_user_at 构建 reader。"""
    def __init__(self, last_user_at):
        self._at = last_user_at

    def make(self):
        at = self._at
        class R:
            def get_last_user_activity(inner_self, principal_id):
                return at
        return R()


# ── admission matrix ──


class TestAdmissionMatrix:
    def test_empty_db_admits(self, memory_db):
        """空库（无任何活动）→ admit。"""
        r = admit(memory_db)
        assert r.admit is True
        assert r.reasons == []

    def test_active_turn_denies(self, memory_db):
        _seed_turn(memory_db, status="running")
        r = admit(memory_db)
        assert r.admit is False
        assert "active_turn" in r.reasons

    def test_queued_turn_denies(self, memory_db):
        _seed_turn(memory_db, status="queued")
        r = admit(memory_db)
        assert r.admit is False
        assert "active_turn" in r.reasons

    def test_high_priority_backlog_denies(self, memory_db):
        _seed_task(memory_db, priority=60, status="queued")
        r = admit(memory_db)
        assert r.admit is False
        assert "priority_backlog" in r.reasons

    def test_low_priority_task_does_not_block(self, memory_db):
        """priority < 50 不计入 high-priority backlog。"""
        _seed_task(memory_db, priority=30, status="queued")
        r = admit(memory_db)
        assert r.admit is True

    def test_delivery_backlog_denies(self, memory_db):
        _seed_delivery(memory_db, status="pending")
        r = admit(memory_db)
        assert r.admit is False
        assert "delivery_backlog" in r.reasons

    def test_outbox_critical_denies(self, memory_db):
        """pending outbox age >= 阈值 → deny。"""
        _seed_outbox(memory_db, age_ms=10_000)  # 10s > 默认 5s 阈值
        r = admit(memory_db)
        assert r.admit is False
        assert "outbox_critical" in r.reasons

    def test_outbox_fresh_does_not_block(self, memory_db):
        """pending outbox age < 阈值 → 不因此 deny。"""
        _seed_outbox(memory_db, age_ms=1_000)  # 1s < 5s
        r = admit(memory_db)
        assert r.admit is True

    def test_budget_exhausted_denies(self, memory_db):
        """当日 run 数已达 max → deny。"""
        for i in range(3):
            _seed_drift_run(memory_db, _uniq("dr"), None, status="completed")
        r = admit(memory_db)
        assert r.admit is False
        assert "budget_exhausted" in r.reasons

    def test_drift_already_active_denies(self, memory_db):
        """已有 active Drift → deny。"""
        _seed_drift_run(memory_db, "dr-active", None, status="running")
        r = admit(memory_db)
        assert r.admit is False
        assert "drift_already_active" in r.reasons

    def test_not_idle_long_enough_denies(self, memory_db):
        """用户最近活动未超 idle_after → deny。"""
        reader = _ReaderFactory(datetime.now(UTC) - timedelta(minutes=5)).make()
        r = admit(memory_db, idle_after_minutes=30, presence_reader=reader)
        assert r.admit is False
        assert "not_idle_long_enough" in r.reasons

    def test_idle_long_enough_admits(self, memory_db):
        """用户活动超 idle_after → 不因此 deny。"""
        reader = _ReaderFactory(datetime.now(UTC) - timedelta(hours=2)).make()
        r = admit(memory_db, idle_after_minutes=30, presence_reader=reader)
        assert r.admit is True

    def test_multiple_reasons_collected(self, memory_db):
        """多个不满足条件 → reasons 包含所有 deny 原因。"""
        _seed_turn(memory_db, status="running")
        _seed_delivery(memory_db, status="pending")
        r = admit(memory_db)
        assert r.admit is False
        assert "active_turn" in r.reasons
        assert "delivery_backlog" in r.reasons

    def test_presence_reader_failure_tolerated(self, memory_db):
        """reader 抛异常 → 不崩溃，last_activity 视为 None。"""
        class BoomReader:
            def get_last_user_activity(self, principal_id):
                raise RuntimeError("db down")
        # 未活动(None) → not_idle_long_enough (age=None 不触发) → 其他满足 → admit
        r = admit(memory_db, presence_reader=BoomReader())
        assert r.admit is True


# ── snapshot ──


class TestAdmissionSnapshot:
    def test_snapshot_populated(self, memory_db):
        _seed_turn(memory_db, status="running")
        r = admit(memory_db)
        assert r.snapshot.active_normal_turns == 1
        assert r.snapshot.snapshot_at > 0

    def test_snapshot_to_dict_roundtrip(self, memory_db):
        r = admit(memory_db)
        d = r.snapshot.to_dict()
        assert "active_normal_turns" in d
        assert "snapshot_at" in d