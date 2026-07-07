"""Tests for MemoryService — 长期记忆服务。

覆盖场景：
- remember 创建新记忆
- remember 幂等（同值不创建新）
- remember 覆盖（不同值 supersede）
- retrieve 按 principal 隔离
- forget 软删除
- propose + confirm 流程
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.domain.memory import MemoryItem, MemoryKind, MemoryStatus
from cogito.service.memory_service import SqliteMemoryService, _make_canonical_key
from cogito.store.migration import migrate


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def service(db) -> SqliteMemoryService:
    return SqliteMemoryService(db)


class TestMemoryService:
    def test_remember_creates_confirmed(self, service):
        m = service.remember(
            kind="preference",
            subject="user",
            predicate="preferred_language",
            value="Python",
            principal_id="p1",
        )
        assert m.status == MemoryStatus.confirmed
        assert m.principal_id == "p1"
        assert m.canonical_key == "p1.user.preferred_language"

    def test_remember_idempotent_same_value(self, service):
        m1 = service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        m2 = service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        # 同值返回同一记忆
        assert m1.memory_id == m2.memory_id

    def test_remember_supersedes_old_value(self, service):
        m1 = service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        m2 = service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Rust",
            principal_id="p1",
        )
        # 新值创建新记忆
        assert m1.memory_id != m2.memory_id
        # 查询应只返回新值
        results = service.retrieve(principal_id="p1", query="Rust")
        assert any("Rust" in r.value for r in results)

    def test_retrieve_by_principal_isolation(self, service):
        service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Rust",
            principal_id="p2",
        )

        p1_results = service.retrieve(principal_id="p1")
        assert len(p1_results) == 1
        assert p1_results[0].value == "Python"

        p2_results = service.retrieve(principal_id="p2")
        assert len(p2_results) == 1
        assert p2_results[0].value == "Rust"

    def test_retrieve_with_query(self, service):
        service.remember(
            kind="fact",
            subject="project",
            predicate="database",
            value="PostgreSQL",
            principal_id="p1",
        )
        results = service.retrieve(principal_id="p1", query="PostgreSQL")
        assert len(results) > 0
        assert "PostgreSQL" in results[0].value

    def test_retrieve_with_query_no_match(self, service):
        service.remember(
            kind="fact",
            subject="project",
            predicate="database",
            value="PostgreSQL",
            principal_id="p1",
        )
        results = service.retrieve(principal_id="p1", query="MySQL")
        assert len(results) == 0

    def test_forget(self, service):
        m = service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        ok = service.forget(m.memory_id)
        assert ok is True

        # 不应再被检索到
        results = service.retrieve(principal_id="p1", query="Python")
        assert len(results) == 0

    def test_forget_nonexistent(self, service):
        ok = service.forget("nonexistent")
        assert ok is False

    def test_forget_by_canonical_key(self, service):
        service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        ok = service.forget_by_canonical_key("p1", "user", "lang")
        assert ok is True

        results = service.retrieve(principal_id="p1", query="Python")
        assert len(results) == 0

    def test_propose_then_confirm(self, service):
        m = service.propose(
            kind="fact",
            subject="project",
            predicate="name",
            value="Cogito",
            principal_id="p1",
        )
        assert m.status == MemoryStatus.candidate

        ok = service.confirm(m.memory_id, confirmed_by="p1")
        assert ok is True

        got = service.get(m.memory_id)
        assert got is not None
        assert got.status == MemoryStatus.confirmed

    def test_propose_then_reject(self, service):
        m = service.propose(
            kind="fact",
            subject="project",
            predicate="name",
            value="Cogito",
            principal_id="p1",
        )
        ok = service.reject(m.memory_id)
        assert ok is True

        got = service.get(m.memory_id)
        assert got.status == MemoryStatus.rejected

    def test_get_returns_memory(self, service):
        m = service.remember(
            kind="fact",
            subject="user",
            predicate="name",
            value="Alice",
            principal_id="p1",
        )
        got = service.get(m.memory_id)
        assert got is not None
        assert got.value == "Alice"

    def test_get_nonexistent(self, service):
        assert service.get("nonexistent") is None

    def test_remember_with_scope(self, service):
        m = service.remember(
            kind="fact",
            subject="project",
            predicate="tech",
            value="Python",
            principal_id="p1",
            scope_type="conversation",
            scope_id="conv_1",
        )
        assert m.scope_type == "conversation"
        assert m.scope_id == "conv_1"

        # 不同 scope 不冲突
        m2 = service.remember(
            kind="fact",
            subject="project",
            predicate="tech",
            value="Rust",
            principal_id="p1",
            scope_type="conversation",
            scope_id="conv_2",
        )
        assert m.memory_id != m2.memory_id


class TestCanonicalKey:
    def test_with_subject_predicate(self):
        key = _make_canonical_key("p1", "user", "likes")
        assert key == "p1.user.likes"

    def test_empty_subject_predicate(self):
        key = _make_canonical_key("p1", "", "", value="some value")
        assert key.startswith("p1.hash.")
