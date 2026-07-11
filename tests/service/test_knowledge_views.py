"""P13-13: KNOWLEDGE.md view + Explain tests."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from cogito.service.knowledge_views import KnowledgeViewsGenerator


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


class TestKnowledgeViews:
    def test_generate_empty(self, db):
        with tempfile.TemporaryDirectory() as td:
            gen = KnowledgeViewsGenerator(db, workspace_path=td)
            gen.generate_all()
            content = Path(td, "knowledge", "KNOWLEDGE.md").read_text(encoding="utf-8")
            assert "暂无知识资源" in content

    def test_generate_with_resources(self, db):
        from cogito.domain.knowledge import KnowledgeResource, ResourceStatus
        from cogito.store import knowledge_repo
        r = KnowledgeResource(
            source_kind="explicit_local_file",
            source_uri_hash="abcdefghijklmnop1234567890",
            content_hash="content-1",
            trust_label="high",
            status=ResourceStatus.active.value,
        )
        knowledge_repo.insert_resource(db, r)
        db.commit()

        with tempfile.TemporaryDirectory() as td:
            gen = KnowledgeViewsGenerator(db, workspace_path=td)
            gen.generate_all()
            content = Path(td, "knowledge", "KNOWLEDGE.md").read_text(encoding="utf-8")
            assert "active" in content
            assert "explicit_local_file" in content
            # API 不泄漏完整 hash（PLAN-13 §14.5）
            assert "abcdefghijklmnop1234567890" not in content
            assert "abcdefghijklmnop" in content  # 仅前 16 字符

    def test_view_failure_no_rollback(self, db):
        """View 失败不回滚数据库事务（PLAN-13 §14.5）。"""
        # 用一个无效路径触发异常
        gen = KnowledgeViewsGenerator(db, workspace_path="/nonexistent/invalid/path/xyz")
        gen.generate_all()  # 不应抛异常
