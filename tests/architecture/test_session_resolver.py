"""SessionResolver + ContextSnapshot + Lane aging — Plan 02 M5."""

from __future__ import annotations

import pytest

from cogito.runtime.context import (
    ContextItem,
    ContextSnapshot,
)
from cogito.service.session_resolver import SessionResolver


# ---------------------------------------------------------------------------
# ContextItem / ContextSnapshot new fields
# ---------------------------------------------------------------------------


def test_context_item_has_score_and_retrieval_path() -> None:
    item = ContextItem(
        item_type="memory",
        item_id="m1",
        source="sess-1",
        score=0.85,
        retrieval_path="keyword+vector",
    )
    assert item.score == 0.85
    assert item.retrieval_path == "keyword+vector"


def test_context_snapshot_has_query_plan_version() -> None:
    snap = ContextSnapshot(query_plan_version="2")
    assert snap.query_plan_version == "2"
    # 默认值不影响反序列化
    snap2 = ContextSnapshot()
    assert snap2.query_plan_version == "1"


# ---------------------------------------------------------------------------
# SessionResolver
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Any:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate

    migrate(conn)
    return conn


def test_resolver_creates_new_session(db: Any) -> None:
    resolver = SessionResolver(db)
    res = resolver.resolve(
        channel_type="web",
        channel_instance_id="web-1",
        conversation_ref="conv-1",
        principal_id="owner",
    )
    assert res.session_id
    assert res.is_new_generation is True
    assert "principal:owner" in res.context_partition_key
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_resolver_reuses_existing_session(db: Any) -> None:
    """同一 partition 复用 Session，不创建新 generation。"""
    resolver = SessionResolver(db)
    first = resolver.resolve(
        channel_type="web",
        channel_instance_id="web-1",
        conversation_ref="conv-1",
        principal_id="owner",
    )
    second = resolver.resolve(
        channel_type="web",
        channel_instance_id="web-1",
        conversation_ref="conv-1",
        principal_id="owner",
    )
    assert first.session_id == second.session_id
    assert second.is_new_generation is False


def test_resolver_resets_generation(db: Any) -> None:
    """reset_generation > 0 创建新 Session generation。"""
    resolver = SessionResolver(db)
    first = resolver.resolve(
        channel_type="web",
        channel_instance_id="web-1",
        conversation_ref="conv-1",
        principal_id="owner",
    )
    second = resolver.resolve(
        channel_type="web",
        channel_instance_id="web-1",
        conversation_ref="conv-1",
        principal_id="owner",
        reset_generation=1,
    )
    assert first.session_id != second.session_id
    assert second.is_new_generation is True
    assert second.reset_generation == 1


def test_resolver_isolates_by_principal(db: Any) -> None:
    """不同 Principal 永远独立 Session（即使同一 conversation）。"""
    resolver = SessionResolver(db)
    a = resolver.resolve(
        channel_type="web",
        channel_instance_id="web-1",
        conversation_ref="grp-1",
        principal_id="user-a",
    )
    b = resolver.resolve(
        channel_type="web",
        channel_instance_id="web-1",
        conversation_ref="grp-1",
        principal_id="user-b",
    )
    assert a.session_id != b.session_id


from typing import Any  # noqa: E402
