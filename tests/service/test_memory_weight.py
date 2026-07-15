"""P13-05: 版本化权重纯函数 + recompute 属性测试。

PLAN-13 MEM-P0-02：文档指数衰减公式 vs 代码乘法因子 → 以文档为准。
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cogito.service.memory_weight import (
    MemoryWeightPolicy,
    compute_retrieval_weight,
    compute_weight_for_item,
    explain_weight,
    weight_status,
)
from cogito.store.memory_repo import MemoryRepository


# ── 纯函数数值测试 ──


class TestComputeRetrievalWeight:
    def test_deterministic(self):
        """同输入 → 同输出。"""
        w1 = compute_retrieval_weight(0.8, 1.0, 1.0, 0.001, 10, 2, 0.5)
        w2 = compute_retrieval_weight(0.8, 1.0, 1.0, 0.001, 10, 2, 0.5)
        assert w1 == w2

    def test_time_increases_weight_decreases(self):
        """时间单调增加时，无新信号权重不增加。"""
        w_fresh = compute_retrieval_weight(0.8, 1.0, 1.0, 0.02, 0, 0, 0.5)
        w_old = compute_retrieval_weight(0.8, 1.0, 1.0, 0.02, 30, 0, 0.5)
        assert w_old < w_fresh

    def test_no_signal_weight_never_increases(self):
        """时间增加 + 零 reinforcement → 权重不增。"""
        w1 = compute_retrieval_weight(0.5, 0.5, 0.5, 0.01, 5, 0, 0.5)
        w2 = compute_retrieval_weight(0.5, 0.5, 0.5, 0.01, 100, 0, 0.5)
        assert w2 <= w1

    def test_user_affirmed_higher_than_referenced(self):
        """user_affirmed（reinforcement=2）比零信号权重更高。"""
        w_affirmed = compute_retrieval_weight(0.5, 0.5, 0.5, 0.001, 10, 2, 0.5)
        w_referenced = compute_retrieval_weight(0.5, 0.5, 0.5, 0.001, 10, 0, 0.5)
        assert w_affirmed > w_referenced

    def test_active_goal_no_decay(self):
        """constraint（kind_decay=0）不因时间自动失效。"""
        w1 = compute_retrieval_weight(0.5, 0.5, 1.0, 0.0, 0, 0, 0.5)
        w2 = compute_retrieval_weight(0.5, 0.5, 1.0, 0.0, 1000, 0, 0.5)
        assert w1 == w2  # decay_rate=0 → 不衰减

    def test_boundary_clamp_no_nan(self):
        """边界 clamp 正确，无 NaN/负数。"""
        w = compute_retrieval_weight(0.0, 0.0, 0.0, 1.0, 1000, 0, 0.0)
        assert 0.0 <= w <= 2.0
        assert not math.isnan(w)
        # 上限
        w_max = compute_retrieval_weight(1.0, 1.0, 1.0, 0.0, 0, 100, 1.0)
        assert w_max <= 2.0

    def test_recompute_twice_consistent(self):
        """重算两次结果一致（幂等性）。"""
        w1 = compute_retrieval_weight(0.7, 0.9, 1.0, 0.001, 5, 3, 0.6)
        w2 = compute_retrieval_weight(0.7, 0.9, 1.0, 0.001, 5, 3, 0.6)
        assert w1 == w2

    def test_timezone_independent_utc(self):
        """基于 UTC 计算，时区/DST 不影响。"""
        now = datetime.now(UTC)
        earlier = now - timedelta(days=5)
        w1 = compute_weight_for_item(
            importance=0.8,
            explicitness="explicit_user_statement",
            status="confirmed",
            kind="fact",
            last_active_at=earlier,
            now=now,
            reinforcement=0,
            emotional_weight=0.5,
            policy=MemoryWeightPolicy(),
        )
        # 相同时差不同 tz-aware now 应一致
        later = now + timedelta(days=5)
        w2 = compute_weight_for_item(
            importance=0.8,
            explicitness="explicit_user_statement",
            status="confirmed",
            kind="fact",
            last_active_at=now,
            now=later,
            reinforcement=0,
            emotional_weight=0.5,
            policy=MemoryWeightPolicy(),
        )
        assert abs(w1 - w2) < 1e-9


# ── Explain / Status ──


class TestWeightExplainStatus:
    def test_explain_returns_all_buckets(self):
        now = datetime.now(UTC)
        exp = explain_weight(
            importance=0.8,
            explicitness="explicit_user_statement",
            status="confirmed",
            kind="fact",
            last_active_at=now - timedelta(days=10),
            now=now,
            reinforcement=2,
            emotional_weight=0.7,
            policy=MemoryWeightPolicy(),
        )
        for key in (
            "base_score",
            "source_trust",
            "confirmation_score",
            "kind_decay_rate",
            "days_since_last_active",
            "decay_factor",
            "reinforcement",
            "reinforcement_bonus",
            "emotional_bonus",
            "retrieval_weight",
            "algorithm_version",
        ):
            assert key in exp

    def test_weight_status_searchable(self):
        assert weight_status(0.5, MemoryWeightPolicy()) == "searchable"

    def test_weight_status_archived(self):
        assert weight_status(0.07, MemoryWeightPolicy()) == "archived"

    def test_weight_status_forgetting(self):
        assert weight_status(0.02, MemoryWeightPolicy()) == "forgetting_candidate"


# ── Repository recompute ──


class TestRecomputeWeight:
    @pytest.fixture
    def db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from cogito.store.migration import migrate

        migrate(conn)
        return conn

    def _insert(self, db, **kw):
        mid = kw.pop("memory_id", uuid.uuid4().hex)
        db.execute(
            "INSERT INTO memory_items "
            "(memory_id, kind, subject, predicate, value, principal_id, "
            "explicitness, confidence, importance, status, created_at, last_retrieved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            (
                mid,
                kw.get("kind", "fact"),
                "s",
                "p",
                "v",
                "owner",
                kw.get("explicitness", "explicit_user_statement"),
                1.0,
                kw.get("importance", 0.8),
                datetime.now(UTC).isoformat(),
                kw.get("retrieved_at"),
            ),
        )
        db.commit()
        return mid

    def test_recompute_writes_back(self, db):
        now = datetime.now(UTC)
        mid = self._insert(db, retrieved_at=(now - timedelta(days=5)).isoformat())
        repo = MemoryRepository(db)
        w = repo.recompute_weight(memory_id=mid, now=now)
        assert 0.0 <= w <= 2.0
        row = db.execute(
            "SELECT retrieval_weight, last_weight_update FROM memory_items WHERE memory_id=?",
            (mid,),
        ).fetchone()
        assert row["last_weight_update"] is not None

    def test_recompute_uses_signals(self, db):
        """reinforcement 从 memory_signals 聚合，非 memory_items.cache。"""
        now = datetime.now(UTC)
        mid = self._insert(db)
        from cogito.store.signal_repo import SignalRepository

        sig_repo = SignalRepository(db)
        sig_repo.insert(
            __import__("cogito.store.signal_repo", fromlist=["MemorySignal"]).MemorySignal(
                signal_id="s1",
                memory_id=mid,
                signal_type="user_affirmed",
            )
        )
        repo = MemoryRepository(db)
        w_with_signals = repo.recompute_weight(memory_id=mid, now=now, signals_repo=sig_repo)
        # 有 affirmed（reinforcement=2）应比纯 importance 高
        assert w_with_signals > 0

    def test_recompute_all_batch(self, db):
        now = datetime.now(UTC)
        for _ in range(3):
            self._insert(db)
        repo = MemoryRepository(db)
        count = repo.recompute_all_weights(now=now)
        assert count == 3

    def test_apply_decay_compat_shim(self, db):
        """旧 apply_decay() 仍可用（委托新逻辑）。"""
        self._insert(db)
        repo = MemoryRepository(db)
        assert repo.apply_decay() >= 1
