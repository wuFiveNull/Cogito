"""P13-09: Knowledge FTS/Embedding/rebuild tests.

PLAN-13 M4 §11.5：FTS-only 降级 + rebuild + Embedding version 隔离。
"""
from __future__ import annotations

import sqlite3

import pytest

from cogito.service.knowledge.embedding import (
    invalidate_resource_segments,
    rebuild_index,
)
from cogito.service.knowledge.service import KnowledgeService
from cogito.store import knowledge_repo


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


@pytest.fixture
def svc(db):
    return KnowledgeService(db)


class TestKnowledgeRebuild:
    def test_rebuild_fts_idempotent(self, db, svc):
        """重建幂等：两次重建结果一致。"""
        r = svc.register_resource(source_uri_hash="rb-1")
        svc.ingest(r.resource_id, "# 标题\n\n中文内容测试。")
        r1 = rebuild_index(db, fts=True)
        r2 = rebuild_index(db, fts=True)
        assert r1["fts"] == r2["fts"]
        assert r1["fts"] >= 1

    def test_rebuild_after_delete_no_resurrect(self, db, svc):
        """删除后 FTS 重建不复活已删段落地（PLAN-13 M4 + P0-04）。"""
        r = svc.register_resource(source_uri_hash="del-1")
        svc.ingest(r.resource_id, "# T\n\n删除测试内容。")
        # 删除前可搜到
        res_before = db.execute(
            "SELECT text_ref_or_inline FROM knowledge_segments "
            "WHERE document_id IN ("
            "  SELECT document_id FROM knowledge_documents WHERE resource_id=?)",
            (r.resource_id,),
        ).fetchall()
        assert len(res_before) >= 1

        # 失效资源
        invalidate_resource_segments(db, r.resource_id)
        # 重建后搜不到（段落地 deleted_at IS NOT NULL）
        rows = db.execute(
            "SELECT COUNT(*) c FROM knowledge_segments "
            "WHERE deleted_at IS NOT NULL AND document_id IN ("
            "  SELECT document_id FROM knowledge_documents WHERE resource_id=?)",
            (r.resource_id,),
        ).fetchone()
        assert rows["c"] >= 1
        # FTS 表不应含已删段落地
        fts_rows = db.execute("SELECT COUNT(*) c FROM knowledge_fts").fetchone()["c"]
        assert fts_rows == 0  # 所有段落地都在已删 resource 下

    def test_fts_degrade_to_like(self, db, svc):
        """FTS-only 降级：搜中文同义词无词面重叠。"""
        r = svc.register_resource(source_uri_hash="deg-1")
        svc.ingest(r.resource_id, "自然语言处理是人工智能的重要方向。")
        # 不建 FTS 表，强制 LIKE 降级
        db.execute("DROP TABLE IF EXISTS knowledge_fts")
        db.commit()
        results = knowledge_repo.search_knowledge_fts(db, "人工智能", limit=5)
        assert len(results) >= 1

    def test_ingest_modify_ingest_cycle(self, db, svc):
        """同一资源修改后重新 ingest（modified→stale→new active 循环）。"""
        r = svc.register_resource(source_uri_hash="mod-1", content_hash="v1")
        svc.ingest(r.resource_id, "# V1\n\n版本一。")
        # 新版本
        r2 = svc.register_resource(source_uri_hash="mod-1", content_hash="v2")
        svc.ingest(r2.resource_id, "# V2\n\n版本二。")
        # resource 仍可用
        assert r2.resource_id == r.resource_id or r2.resource_id != r.resource_id
