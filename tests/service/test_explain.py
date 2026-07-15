"""P13-13: ExplainMemoryWeight + ListMemorySources tests."""

from __future__ import annotations

import sqlite3

import pytest

from cogito.service.explain import ExplainService


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate

    migrate(conn)
    return conn


class TestExplainService:
    def test_explain_memory_weight(self, db):
        from cogito.service.memory_service import SqliteMemoryService

        svc = SqliteMemoryService(db)
        mem = svc.remember(
            kind="fact", subject="user", predicate="lang", value="Python", principal_id="owner"
        )
        explain = ExplainService(db)
        result = explain.explain_memory_weight(mem.memory_id)
        assert result is not None
        assert "base_score" in result
        assert "retrieval_weight" in result
        assert result["algorithm_version"] == "2"

    def test_list_memory_sources(self, db):
        from cogito.service.memory_service import SqliteMemoryService

        svc = SqliteMemoryService(db)
        mem = svc.remember(
            kind="fact", subject="user", predicate="lang", value="Python", principal_id="owner"
        )
        explain = ExplainService(db)
        sources = explain.list_memory_sources(mem.memory_id)
        assert len(sources) >= 1
        assert sources[0]["source_type"] == "manual"

    def test_get_memory_detail(self, db):
        from cogito.service.memory_service import SqliteMemoryService

        svc = SqliteMemoryService(db)
        mem = svc.remember(
            kind="fact", subject="user", predicate="lang", value="Python", principal_id="owner"
        )
        explain = ExplainService(db)
        detail = explain.get_memory_detail(mem.memory_id)
        assert detail is not None
        assert detail["memory_id"] == mem.memory_id
        assert detail["status"] == "confirmed"

    def test_explain_nonexistent(self, db):
        explain = ExplainService(db)
        assert explain.explain_memory_weight("nonexistent") is None
        assert explain.get_memory_detail("nonexistent") is None
