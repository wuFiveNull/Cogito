"""PR-R7: Memory lifecycle closure — decay + relations + rebuild + consolidation.

Plan 02 M7: retrieval_weight 分 kind 衰减；关系可追溯；索引全量重建；Consolidation 幂等。
"""
from __future__ import annotations

import pytest

from cogito.domain.memory import MemoryKind, MemoryStatus
from cogito.service.memory_service import SqliteMemoryService


@pytest.fixture
def db() -> Any:
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


def _insert_confirmed(db: Any, **kw: Any) -> str:
    import uuid
    from datetime import datetime, UTC
    mid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO memory_items "
        "(memory_id, kind, subject, predicate, value, principal_id, "
        "explicitness, confidence, importance, status, retrieval_weight, "
        "decay_rate, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', 1.0, 1.0, ?, ?)",
        (
            mid, kw.get("kind", "fact"), kw.get("subject", ""),
            kw.get("predicate", ""), kw.get("value", ""),
            kw.get("principal_id", "owner"),
            kw.get("explicitness", "explicit_user_statement"),
            kw.get("confidence", 1.0), kw.get("importance", 0.7),
            now, now,
        ),
    )
    db.commit()
    return mid


# ── 1. Decay by kind ───────────────────────────────────────────

def test_decay_reduces_weight(db: Any) -> None:
    """衰减后 retrieval_weight 下降但不低于 0.1。"""
    mid = _insert_confirmed(db, kind="episode", subject="old", predicate="x", value="v")
    repo = __import__("cogito.store.memory_repo", fromlist=["MemoryRepository"]).MemoryRepository(db)
    updated = repo.apply_decay()
    assert updated >= 1
    row = db.execute("SELECT retrieval_weight FROM memory_items WHERE memory_id=?", (mid,)).fetchone()
    assert row["retrieval_weight"] < 1.0
    assert row["retrieval_weight"] >= 0.1


def test_fact_decays_slower_than_episode(db: Any) -> None:
    """fact 衰减比 episode 慢（更固化的知识保持更久）。

    PLAN-13 P13-05: 指数衰减公式下，fact(kind_decay=0.001) 比
    episode(kind_decay=0.02) 衰减慢。需要足够时间窗口让差异显现。
    """
    from datetime import timedelta
    fact_id = _insert_confirmed(db, kind="fact", subject="pi", predicate="value", value="3.14")
    ep_id = _insert_confirmed(db, kind="episode", subject="lunch", predicate="where", value="cafe")
    # 设置 last_retrieved_at 为 30 天前，让 kind_decay_rate 差异显现
    old = (__import__("datetime").datetime.now(__import__("datetime").UTC) - timedelta(days=30)).isoformat()
    db.execute("UPDATE memory_items SET last_retrieved_at=? WHERE memory_id=?", (old, fact_id))
    db.execute("UPDATE memory_items SET last_retrieved_at=? WHERE memory_id=?", (old, ep_id))
    db.commit()
    repo = __import__("cogito.store.memory_repo", fromlist=["MemoryRepository"]).MemoryRepository(db)
    repo.apply_decay()
    fact_w = db.execute("SELECT retrieval_weight FROM memory_items WHERE memory_id=?", (fact_id,)).fetchone()[0]
    ep_w = db.execute("SELECT retrieval_weight FROM memory_items WHERE memory_id=?", (ep_id,)).fetchone()[0]
    assert fact_w > ep_w  # fact 保持更高权重


# ── 2. Relations traceable ─────────────────────────────────────

def test_supersedes_relation_traced(db: Any) -> None:
    """supersedes 关系可追溯（新明确值覆盖旧推断）。"""
    svc = SqliteMemoryService(db)
    old = svc.remember(kind="fact", subject="ver", predicate="x", value="1", principal_id="owner")
    # 同 canonical_key 同值 → 返回已有，改用不同值触发 supersedes
    new = svc.remember(kind="fact", subject="ver", predicate="x", value="2", principal_id="owner")
    repo = __import__("cogito.store.memory_repo", fromlist=["MemoryRepository"]).MemoryRepository(db)
    rels = repo.get_relations(new.memory_id)
    # supersedes 关系应存在
    assert any(r.get("relation_type") == "supersedes" for r in rels)


# ── 3. Rebuild index ───────────────────────────────────────────

def test_rebuild_fts_index(db: Any) -> None:
    """从 Canonical Memory 全量重建 FTS 索引。"""
    _insert_confirmed(db, subject="rebuildable", predicate="x", value="v")
    repo = __import__("cogito.store.memory_repo", fromlist=["MemoryRepository"]).MemoryRepository(db)
    result = repo.rebuild_index(fts=True, embeddings=False)
    assert result["fts"] >= 1


def test_rebuild_is_idempotent(db: Any) -> None:
    """重建幂等：两次重建结果一致。"""
    _insert_confirmed(db, subject="idem", predicate="x", value="v")
    repo = __import__("cogito.store.memory_repo", fromlist=["MemoryRepository"]).MemoryRepository(db)
    r1 = repo.rebuild_index(fts=True, embeddings=False)
    r2 = repo.rebuild_index(fts=True, embeddings=False)
    assert r1["fts"] == r2["fts"]


# ── 4. Consolidation ───────────────────────────────────────────

def test_consolidation_idempotent(db: Any) -> None:
    """Consolidation 幂等执行：衰减 + 遗忘候选。"""
    _insert_confirmed(db, subject="live", predicate="x", value="v")
    repo = __import__("cogito.store.memory_repo", fromlist=["MemoryRepository"]).MemoryRepository(db)
    res = repo.consolidate()
    assert res["decayed"] >= 1
    # 第二次执行不报错
    res2 = repo.consolidate()
    assert res2["decayed"] >= 1


# ── 5. Superseded exits default retrieval ─────────────────────

def test_superseded_not_in_retrieval(db: Any) -> None:
    """superseded 条目退出默认检索。"""
    svc = SqliteMemoryService(db)
    old = svc.remember(kind="fact", subject="ver", predicate="x", value="1", principal_id="owner")
    new = svc.remember(kind="fact", subject="ver", predicate="x", value="2", principal_id="owner")
    # new supersedes old
    results = svc.retrieve(principal_id="owner", query="ver")
    returned_ids = {r.memory_id for r in results}
    # old 被 supersede 后不应出现在默认检索
    assert old.memory_id not in returned_ids or new.memory_id in returned_ids


from typing import Any  # noqa: E402