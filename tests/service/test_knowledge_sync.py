"""P13-10: Connector sync + modified/deleted cascade tests."""
from __future__ import annotations

import sqlite3

import pytest

from cogito.service.knowledge.sync import delete_resource, sync_resource


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


class TestKnowledgeSync:
    def test_added(self, db):
        rid = sync_resource(
            db, stable_source_id="src-1", content_hash="h1",
            raw_text="# Hello\n\nWorld.", principal_id="owner",
        )
        assert rid
        row = db.execute(
            "SELECT status FROM knowledge_resources WHERE resource_id=?", (rid,),
        ).fetchone()
        assert row["status"] == "active"

    def test_unchanged_skips(self, db):
        """unchanged 不重新 parse/embed（PLAN-13 M5）。"""
        sync_resource(
            db, stable_source_id="src-2", content_hash="h1",
            raw_text="# T\n\nv1.", principal_id="owner",
        )
        cnt1 = db.execute("SELECT COUNT(*) c FROM knowledge_segments").fetchone()["c"]
        # 同步同版本
        sync_resource(
            db, stable_source_id="src-2", content_hash="h1",
            raw_text="# T\n\nv2 different text.", principal_id="owner",
        )
        cnt2 = db.execute("SELECT COUNT(*) c FROM knowledge_segments").fetchone()["c"]
        assert cnt1 == cnt2  # 未新增段落地

    def test_modified_replaces(self, db):
        """modified 只替换受影响版本（PLAN-13 M5）。"""
        sync_resource(
            db, stable_source_id="src-3", content_hash="v1",
            raw_text="# V1\n\n版本一。", principal_id="owner",
        )
        # 修改后重新同步
        rid2 = sync_resource(
            db, stable_source_id="src-3", content_hash="v2",
            raw_text="# V2\n\n版本二。", principal_id="owner",
        )
        # resource 被替换（新 ID 或新 active document）
        docs = db.execute(
            "SELECT COUNT(*) c FROM knowledge_documents WHERE resource_id=? AND status='active'",
            (rid2,),
        ).fetchone()["c"]
        assert docs >= 1
        # 旧 resource 标 stale
        stale = db.execute(
            "SELECT COUNT(*) c FROM knowledge_resources WHERE source_uri_hash='src-3' AND status='stale'",
        ).fetchone()["c"]
        assert stale >= 1

    def test_deleted_no_resurrection(self, db):
        """deleted 后 FTS 不可命中正文（PLAN-13 P0-04）。"""
        from cogito.store import knowledge_repo
        sync_resource(
            db, stable_source_id="src-4", content_hash="d1",
            raw_text="# DeleteMe\n\n删除测试。", principal_id="owner",
        )
        ensure_fts = knowledge_repo.ensure_knowledge_fts(db)
        # 删除
        assert delete_resource(db, stable_source_id="src-4", principal_id="owner")
        # 搜索结果为空
        if ensure_fts:
            results = knowledge_repo.search_knowledge_fts(db, "删除测试")
            assert len(results) == 0

    def test_deleted_idempotent(self, db):
        """同一删除事件重放幂等（PLAN-13 M5）。"""
        sync_resource(
            db, stable_source_id="src-5", content_hash="d1",
            raw_text="# X", principal_id="owner",
        )
        assert delete_resource(db, stable_source_id="src-5", principal_id="owner")
        assert delete_resource(db, stable_source_id="src-5", principal_id="owner")

    def test_delete_nonexistent(self, db):
        assert delete_resource(db, stable_source_id="nonexistent", principal_id="owner")
