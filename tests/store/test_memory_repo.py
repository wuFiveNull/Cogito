"""Tests for MemoryRepository — CRUD 操作验证。

覆盖场景：
- 插入和查询
- Principal 隔离
- 状态转换
- 乐观锁
- 规范键去重
- 超期和删除过滤
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.memory import MemoryItem, MemoryKind, MemoryStatus
from cogito.store.memory_repo import MemoryRepository
from cogito.store.migration import migrate


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def repo(db) -> MemoryRepository:
    return MemoryRepository(db)


def _create_memory(**kwargs) -> MemoryItem:
    """帮助创建测试用 MemoryItem。"""
    defaults = dict(
        memory_id="mem_test",
        kind=MemoryKind.fact,
        subject="user",
        predicate="likes",
        value="Python",
        principal_id="p1",
        canonical_key="p1.user.likes",
        status=MemoryStatus.confirmed,
        confidence=1.0,
        importance=0.5,
    )
    defaults.update(kwargs)
    return MemoryItem(**defaults)


class TestMemoryRepository:
    def test_insert_and_get(self, repo):
        m = _create_memory()
        repo.insert(m)

        got = repo.get(m.memory_id)
        assert got is not None
        assert got.memory_id == m.memory_id
        assert got.value == "Python"

    def test_get_nonexistent(self, repo):
        assert repo.get("nonexistent") is None

    def test_get_active_filters_deleted(self, repo):
        m = _create_memory(deleted_at=datetime.now(UTC))
        repo.insert(m)

        assert repo.get_active(m.memory_id) is None

    def test_principal_isolation(self, repo):
        m1 = _create_memory(memory_id="m1", principal_id="p1", value="Python")
        m2 = _create_memory(memory_id="m2", principal_id="p2", value="Rust")
        repo.insert(m1)
        repo.insert(m2)

        p1_list = repo.list_confirmed(principal_id="p1")
        assert len(p1_list) == 1
        assert p1_list[0].value == "Python"

        p2_list = repo.list_confirmed(principal_id="p2")
        assert len(p2_list) == 1
        assert p2_list[0].value == "Rust"

    def test_list_confirmed_excludes_expired(self, repo):
        now = datetime.now(UTC)
        m = _create_memory(
            memory_id="m_expired",
            valid_to=now - timedelta(days=1),
        )
        repo.insert(m)

        items = repo.list_confirmed(principal_id="p1")
        assert len(items) == 0

    def test_search_finds_by_value(self, repo):
        m = _create_memory(value="I love Python programming")
        repo.insert(m)

        results = repo.search(principal_id="p1", query="Python")
        assert len(results) >= 1
        assert "Python" in results[0].value

    def test_search_no_match(self, repo):
        m = _create_memory(value="I love Rust")
        repo.insert(m)

        results = repo.search(principal_id="p1", query="Golang")
        assert len(results) == 0

    def test_find_by_canonical_key(self, repo):
        m = _create_memory(canonical_key="p1.user.likes")
        repo.insert(m)

        found = repo.find_by_canonical_key(principal_id="p1", canonical_key="p1.user.likes")
        assert found is not None
        assert found.memory_id == m.memory_id

    def test_insert_sets_created_at_and_version(self, repo):
        m = _create_memory()
        repo.insert(m)

        got = repo.get(m.memory_id)
        assert got is not None
        assert got.version == 1
        assert got.created_at is not None

    def test_optimistic_lock_update(self, repo):
        m = _create_memory(version=1)
        repo.insert(m)

        m.value = "Rust"
        ok = repo.update(m)
        assert ok is True

        # version 冲突
        m.version = 1  # 旧的 version
        ok = repo.update(m)
        assert ok is False

    def test_confirm_transition(self, repo):
        m = _create_memory(
            memory_id="m_cand",
            status=MemoryStatus.candidate,
        )
        repo.insert(m)

        ok = repo.confirm(m.memory_id, confirmed_by="p1")
        assert ok is True

        got = repo.get(m.memory_id)
        assert got is not None
        assert got.status == MemoryStatus.confirmed

    def test_reject_transition(self, repo):
        m = _create_memory(
            memory_id="m_rej",
            status=MemoryStatus.candidate,
        )
        repo.insert(m)

        ok = repo.reject(m.memory_id)
        assert ok is True

        got = repo.get(m.memory_id)
        assert got.status == MemoryStatus.rejected

    def test_expire_transition(self, repo):
        m = _create_memory(memory_id="m_exp")
        repo.insert(m)

        ok = repo.expire(m.memory_id)
        assert ok is True

        got = repo.get(m.memory_id)
        assert got.status == MemoryStatus.expired

    def test_soft_delete(self, repo):
        m = _create_memory()
        repo.insert(m)

        ok = repo.soft_delete(m.memory_id)
        assert ok is True

        got = repo.get(m.memory_id)
        assert got is not None
        assert got.deleted_at is not None

        # get_active 应排除
        assert repo.get_active(m.memory_id) is None

    def test_hard_delete(self, repo):
        m = _create_memory()
        repo.insert(m)

        ok = repo.hard_delete(m.memory_id)
        assert ok is True
        assert repo.get(m.memory_id) is None

    def test_count_active(self, repo):
        m1 = _create_memory(memory_id="m1", value="A")
        m2 = _create_memory(memory_id="m2", value="B")
        repo.insert(m1)
        repo.insert(m2)

        assert repo.count_active(principal_id="p1") == 2

    def test_count_active_excludes_deleted(self, repo):
        m1 = _create_memory(memory_id="m1", value="A")
        repo.insert(m1)
        repo.soft_delete("m1")

        assert repo.count_active(principal_id="p1") == 0

    def test_search_respects_limit(self, repo):
        for i in range(5):
            m = _create_memory(
                memory_id=f"m_{i}",
                value=f"Value {i}",
            )
            repo.insert(m)

        results = repo.search(principal_id="p1", query="Value", limit=2)
        assert len(results) == 2

    def test_list_confirmed_with_kinds_filter(self, repo):
        m1 = _create_memory(
            memory_id="m_fact",
            kind=MemoryKind.fact,
            value="Fact A",
        )
        m2 = _create_memory(
            memory_id="m_pref",
            kind=MemoryKind.preference,
            value="Pref B",
        )
        repo.insert(m1)
        repo.insert(m2)

        results = repo.list_confirmed(principal_id="p1", kinds=["preference"])
        assert len(results) == 1
        assert results[0].kind == MemoryKind.preference
