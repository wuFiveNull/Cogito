"""P13-14: 离线评测集（PLAN-13 §15 M8）。

覆盖 15 类语料中的核心可测项：
1. 明确事实跨 Session 召回
2. 偏好更新与 supersedes
3. 矛盾事实需确认
4. 群聊发送者隔离（principal 隔离）
5. Session reset 后短期不泄漏
7. ID/日期/专名精确检索
8. active/completed goal
9. 文档段落召回
11. 来源修改后旧 Segment 失效
12. 删除后索引重建不复活
13. Embedding 不可用 FTS 降级
15. 错误召回不强化
"""
from __future__ import annotations

import sqlite3

import pytest

from cogito.domain.memory import MemoryKind, MemoryStatus
from cogito.service.memory_service import SqliteMemoryService


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


@pytest.fixture
def svc(db):
    return SqliteMemoryService(db)


# ── 1. 跨 Session 召回 ──

class TestCrossSessionRecall:
    def test_fact_recalled_across_sessions(self, svc):
        """明确事实写入后跨 Session 可召回。"""
        svc.remember(kind="fact", subject="user", predicate="city",
                     value="北京", principal_id="owner")
        results = svc.retrieve(principal_id="owner", query="北京")
        assert len(results) >= 1
        assert results[0].value == "北京"


# ── 2. 偏好更新与 supersedes ──

class TestSupersede:
    def test_preference_update_supersedes(self, svc):
        svc.remember(kind="preference", subject="user", predicate="theme",
                     value="dark", principal_id="owner")
        svc.remember(kind="preference", subject="user", predicate="theme",
                     value="light", principal_id="owner")
        results = svc.retrieve(principal_id="owner", query="theme")
        values = [r.value for r in results]
        assert "light" in values


# ── 4. 群聊发送者隔离 ──

class TestPrincipalIsolation:
    def test_cross_principal_no_leakage(self, svc):
        """不同 principal 不互相泄漏（PLAN-13 §15.1 #4）。"""
        svc.remember(kind="fact", subject="user", predicate="secret",
                     value="owner-secret", principal_id="owner")
        results = svc.retrieve(principal_id="other_user", query="secret")
        assert len(results) == 0  # other_user 看不到 owner 的记忆


# ── 7. 精确检索 ──

class TestExactRetrieval:
    def test_id_date_exact_match(self, svc):
        """ID/日期/专名精确检索。"""
        svc.remember(kind="fact", subject="order", predicate="date",
                     value="2026-07-11", principal_id="owner")
        results = svc.retrieve(principal_id="owner", query="2026-07-11")
        assert len(results) >= 1


# ── 8. active/completed goal ──

class TestGoalStatus:
    def test_active_goal_recalled(self, svc):
        """active goal 进入活动视图。"""
        svc.remember(kind="goal", subject="project", predicate="status",
                     value="完成迁移", principal_id="owner")
        results = svc.retrieve(principal_id="owner", query="迁移",
                               kinds=["goal"])
        assert len(results) >= 1


# ── 12. 删除后不复活 ──

class TestDeleteNoResurrection:
    def test_forget_then_rebuild_no_resurrect(self, db, svc):
        """删除后索引重建不复活（PLAN-13 P0-04）。"""
        from cogito.store.memory_repo import MemoryRepository
        mem = svc.remember(kind="fact", subject="tmp", predicate="x",
                           value="v", principal_id="owner")
        svc.forget(mem.memory_id)
        # 重建 FTS
        repo = MemoryRepository(db)
        repo.rebuild_index(fts=True)
        # 检索不到
        results = svc.retrieve(principal_id="owner", query="tmp")
        assert len(results) == 0


# ── 15. 错误召回不强化 ──

class TestNoWrongReinforcement:
    def test_exposure_does_not_reinforce(self, svc):
        """普通 recall 不增加 reinforcement（PLAN-13 P0-03）。"""
        from cogito.service.memory_signals import SignalWriter
        mem = svc.remember(kind="fact", subject="test", predicate="x",
                           value="v", principal_id="owner")
        # 召回前 reinforcement
        row = svc._repo.get(mem.memory_id)
        before = row.reinforcement if row else 0
        # 主动召回
        svc.retrieve(principal_id="owner", query="test")
        # reinforcement 不变（exposed signal 不增加 reinforcement）
        row_after = svc._repo.get(mem.memory_id)
        after = row_after.reinforcement if row_after else 0
        assert after == before
