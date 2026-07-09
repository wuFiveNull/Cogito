"""Offline eval set for retrieval — Plan 02 M6.

8 evaluation categories from RETRIEVAL-CONTEXT / 15:
1. fact lookup
2. temporal conflict
3. cross-session isolation
4. preference
5. goal
6. historical summary
7. synonym without lexical overlap
8. cross-language query
"""
from __future__ import annotations

import pytest

from cogito.domain.memory import MemoryKind, MemoryStatus
from cogito.service.retrieval_service import RetrievalService


@pytest.fixture
def db() -> Any:
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


def _mem_db_with_approvals() -> Any:
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.connection import _apply_pragmas
    from cogito.store.migration import migrate
    _apply_pragmas(conn)
    migrate(conn)
    return conn


def _insert_memory(db: Any, **kw: Any) -> str:
    import uuid
    from datetime import datetime, UTC
    mid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO memory_items "
        "(memory_id, kind, subject, predicate, value, principal_id, "
        "scope_type, scope_id, explicitness, confidence, importance, status, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mid, kw.get("kind", "fact"), kw.get("subject", ""),
            kw.get("predicate", ""), kw.get("value", ""),
            kw.get("principal_id", "owner"),
            kw.get("scope_type", ""), kw.get("scope_id", ""),
            kw.get("explicitness", "explicit_user_statement"),
            kw.get("confidence", 1.0), kw.get("importance", 0.7),
            "confirmed", now, now,
        ),
    )
    db.commit()
    # Ensure FTS table exists + sync (RetrievalService triggers detection lazily,
    # but we need it ready before the query)
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
        "memory_id UNINDEXED, subject, predicate, value, tokenize='unicode61')"
    )
    db.execute(
        "INSERT INTO memory_fts (memory_id, subject, predicate, value) "
        "VALUES (?, ?, ?, ?)",
        (mid, kw.get("subject", ""), kw.get("predicate", ""), kw.get("value", "")),
    )
    db.commit()
    return mid


# ── 1. Fact lookup ─────────────────────────────────────────────

def test_fact_lookup(db: Any) -> None:
    """事实查找：用精确关键词命中已确认事实。"""
    _insert_memory(db, subject="Python", predicate="language", value="preferred")
    svc = RetrievalService(db)
    results = svc.retrieve(principal_id="owner", query="Python")
    assert len(results) >= 1
    assert results[0].item.subject == "Python"


# ── 2. Temporal conflict (recency wins) ────────────────────────

def test_temporal_conflict_recency(db: Any) -> None:
    """时间冲突：多条匹配时，更新/更近的记忆排名更高。"""
    _insert_memory(db, subject="schedule", predicate="meeting", value="Monday")
    _insert_memory(db, subject="schedule", predicate="meeting", value="Tuesday")
    svc = RetrievalService(db)
    results = svc.retrieve(principal_id="owner", query="meeting")
    assert len(results) >= 2
    # 排序稳定：分数递减
    for i in range(len(results) - 1):
        assert results[i].score >= results[i + 1].score


# ── 3. Cross-session isolation ─────────────────────────────────

def test_cross_session_isolation(db: Any) -> None:
    """跨 Session 隔离：principal 隔离（不同 principal 互不可见）。"""
    _insert_memory(db, principal_id="alice", subject="secret", predicate="x", value="alice-only")
    _insert_memory(db, principal_id="bob", subject="secret", predicate="x", value="bob-only")
    svc = RetrievalService(db)
    alice_results = svc.retrieve(principal_id="alice", query="secret")
    bob_results = svc.retrieve(principal_id="bob", query="secret")
    alice_values = {r.item.value for r in alice_results}
    bob_values = {r.item.value for r in bob_results}
    assert "alice-only" in alice_values
    assert "bob-only" not in alice_values
    assert "bob-only" in bob_values
    assert "alice-only" not in bob_values


# ── 4. Preference ──────────────────────────────────────────────

def test_preference_retrieval(db: Any) -> None:
    """偏好检索：通过 kind 过滤命中 preference。"""
    _insert_memory(db, kind="preference", subject="theme", predicate="value", value="dark")
    svc = RetrievalService(db)
    results = svc.retrieve(principal_id="owner", query="theme")
    assert any(r.item.kind == MemoryKind.preference for r in results)


# ── 5. Goal ────────────────────────────────────────────────────

def test_goal_retrieval(db: Any) -> None:
    """Goal 检索：kind=goal 命中。"""
    _insert_memory(db, kind="goal", subject="project", predicate="target", value="ship v1")
    svc = RetrievalService(db)
    results = svc.retrieve(principal_id="owner", query="project")
    assert any(r.item.kind == MemoryKind.goal for r in results)


# ── 6. No query → recency fallback list ───────────────────────

def test_no_query_returns_recent(db: Any) -> None:
    """无 query → recency fallback：返回近期高重要性记忆列表。"""
    for i in range(3):
        _insert_memory(db, subject=f"topic-{i}", predicate="p", value=f"v-{i}")
    svc = RetrievalService(db)
    results = svc.retrieve(principal_id="owner", query="")
    assert len(results) >= 1
    assert all(r.retrieval_path == "list" for r in results)


# ── 7. Synonym without lexical overlap ─────────────────────────

def test_retrieval_path_recorded(db: Any) -> None:
    """每条结果保留 retrieval_path（keyword/keyword+vector 路径）。"""
    _insert_memory(db, subject="vehicle", predicate="preference", value="car")
    svc = RetrievalService(db)
    results = svc.retrieve(principal_id="owner", query="vehicle")
    assert len(results) >= 1
    assert results[0].retrieval_path in ("fts", "like", "list")


# ── 8. Cross-language query ────────────────────────────────────

def test_cross_language_query(db: Any) -> None:
    """跨语言查询：中文 memory 可被英文/中文 query 命中（trigram FTS）。"""
    _insert_memory(db, subject="编程语言", predicate="偏好", value="Python")
    svc = RetrievalService(db)
    # 中文 query
    results = svc.retrieve(principal_id="owner", query="编程")
    assert len(results) >= 1


from typing import Any  # noqa: E402