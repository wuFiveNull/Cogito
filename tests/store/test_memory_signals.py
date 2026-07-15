"""P13-04: memory_signals + signal writer tests.

PLAN-13 §5.2/§6.1：强化/展示/反馈幂等追加事件表。
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from cogito.service.memory_signals import SignalWriter
from cogito.store.signal_repo import MemorySignal, SignalRepository


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate

    migrate(conn)
    return conn


@pytest.fixture
def writer(db):
    return SignalWriter(db)


def _ensure_memory(db, memory_id: str) -> str:
    """插入一条 confirmed memory（满足 memory_signals FK）。"""
    db.execute(
        "INSERT OR IGNORE INTO memory_items "
        "(memory_id, kind, subject, predicate, value, principal_id, status, created_at) "
        "VALUES (?, 'fact', 's', 'p', 'v', 'owner', 'confirmed', ?)",
        (memory_id, __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
    )
    db.commit()
    return memory_id


@pytest.fixture
def m1(db):
    return _ensure_memory(db, "m1")


@pytest.fixture
def m2(db):
    return _ensure_memory(db, "m2")


@pytest.fixture
def m3(db):
    return _ensure_memory(db, "m3")


class TestSignalRepository:
    def test_insert_and_list(self, db, m1):
        repo = SignalRepository(db)
        s = MemorySignal(signal_id="sig1", memory_id=m1, signal_type="exposed")
        assert repo.insert(s)
        signals = repo.list_for_memory(m1)
        assert len(signals) == 1
        assert signals[0].signal_type == "exposed"

    def test_idempotent_insert(self, db, m1):
        """同 idempotency_key 重复写只产生一条。"""
        repo = SignalRepository(db)
        s1 = MemorySignal(
            signal_id="a",
            memory_id=m1,
            signal_type="exposed",
            idempotency_key="key-1",
        )
        s2 = MemorySignal(
            signal_id="b",
            memory_id=m1,
            signal_type="exposed",
            idempotency_key="key-1",
        )
        repo.insert(s1)
        repo.insert(s2)
        signals = repo.list_for_memory(m1)
        assert len(signals) == 1

    def test_invalid_signal_type_rejected(self, db, m1):
        repo = SignalRepository(db)
        s = MemorySignal(signal_id="x", memory_id=m1, signal_type="invalid")
        assert not repo.insert(s)

    def test_aggregate_reinforcement(self, db, m1):
        """仅 user_affirmed/task_succeeded/user_corrected 贡献 reinforcement。"""
        repo = SignalRepository(db)
        repo.insert(MemorySignal(signal_id="a", memory_id=m1, signal_type="exposed"))
        repo.insert(MemorySignal(signal_id="b", memory_id=m1, signal_type="user_affirmed"))
        repo.insert(MemorySignal(signal_id="c", memory_id=m1, signal_type="task_succeeded"))
        repo.insert(MemorySignal(signal_id="d", memory_id=m1, signal_type="user_corrected"))
        # exposed=0, user_affirmed=+2, task_succeeded=+1, user_corrected=+2 = 5
        assert repo.aggregate_reinforcement(m1) == 5

    def test_negative_feedback_no_negative_overflow(self, db, m1):
        """negative_feedback 不产生负 reinforcement 溢出。"""
        repo = SignalRepository(db)
        repo.insert(MemorySignal(signal_id="a", memory_id=m1, signal_type="negative_feedback"))
        assert repo.aggregate_reinforcement(m1) == 0

    def test_count_by_type(self, db, m1):
        repo = SignalRepository(db)
        repo.insert(MemorySignal(signal_id="a", memory_id=m1, signal_type="exposed"))
        repo.insert(MemorySignal(signal_id="b", memory_id=m1, signal_type="exposed"))
        repo.insert(MemorySignal(signal_id="c", memory_id=m1, signal_type="user_affirmed"))
        counts = repo.count_by_type(m1)
        assert counts["exposed"] == 2
        assert counts["user_affirmed"] == 1


class TestSignalWriter:
    def test_record_exposed_does_not_increase_reinforcement(self, db, writer, m1):
        """exposed 不增加 reinforcement（PLAN-13 P0 误召回不自强化）。"""
        writer.record_exposed(m1)
        writer.record_exposed(m1)
        assert writer.aggregate_reinforcement(m1) == 0

    def test_record_user_affirmed_adds_reinforcement(self, db, writer, m1):
        writer.record_user_affirmed(m1)
        assert writer.aggregate_reinforcement(m1) == 2

    def test_flush_reinforcement_writes_back(self, db, writer):
        mid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO memory_items (memory_id, kind, subject, predicate, value, "
            "principal_id, status, created_at) VALUES (?, 'fact', 's', 'p', 'v', 'owner', 'confirmed', ?)",
            (mid, __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
        )
        db.commit()
        writer.record_user_affirmed(mid)
        writer.record_task_succeeded(mid)
        value = writer.flush_reinforcement(mid)
        assert value == 3  # user_affirmed +2, task_succeeded +1
        row = db.execute(
            "SELECT reinforcement FROM memory_items WHERE memory_id=?", (mid,)
        ).fetchone()
        assert row["reinforcement"] == 3

    def test_record_signal_convenience_methods(self, db, writer, m1, m2, m3):
        assert writer.record_exposed(m1)
        assert writer.record_user_affirmed(m2)
        assert writer.record_task_succeeded(m3)
        assert writer.aggregate_reinforcement(m1) == 0
        assert writer.aggregate_reinforcement(m2) == 2
        assert writer.aggregate_reinforcement(m3) == 1
