"""P13-07: Knowledge schema + repository tests.

PLAN-13 M4 §5.3：Resource/Document/Segment/Embedding CRUD、Scope、软删、索引。
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from cogito.domain.knowledge import (
    KnowledgeDocument,
    KnowledgeResource,
    KnowledgeSegment,
    ResourceStatus,
    SegmentKind,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate

    migrate(conn)
    return conn


class TestKnowledgeResource:
    def test_insert_and_get(self, db):
        from cogito.store.knowledge_repo import insert_resource, get_resource

        r = KnowledgeResource(
            principal_id="owner",
            source_kind="explicit_local_file",
            source_uri_hash="hash-1",
            content_hash="c1",
            status=ResourceStatus.discovered.value,
        )
        insert_resource(db, r)
        got = get_resource(db, r.resource_id)
        assert got is not None
        assert got.source_uri_hash == "hash-1"
        assert got.status == "discovered"

    def test_unique_resource_id(self, db):
        r1 = KnowledgeResource()
        r2 = KnowledgeResource()
        assert r1.resource_id != r2.resource_id


class TestKnowledgeDocument:
    def test_insert_and_list(self, db):
        from cogito.store.knowledge_repo import (
            insert_resource,
            insert_document,
            list_documents_for_resource,
        )

        r = KnowledgeResource()
        insert_resource(db, r)
        doc = KnowledgeDocument(resource_id=r.resource_id, title="测试文档")
        insert_document(db, doc)
        docs = list_documents_for_resource(db, r.resource_id)
        assert len(docs) == 1
        assert docs[0].title == "测试文档"


class TestKnowledgeSegment:
    def test_insert_and_list(self, db):
        from cogito.store.knowledge_repo import (
            insert_resource,
            insert_document,
            insert_segment,
            list_segments_for_document,
        )

        r = KnowledgeResource()
        insert_resource(db, r)
        doc = KnowledgeDocument(resource_id=r.resource_id)
        insert_document(db, doc)
        seg = KnowledgeSegment(
            document_id=doc.document_id,
            ordinal=0,
            segment_kind=SegmentKind.heading.value,
            text_ref_or_inline="# 标题",
            token_count=5,
            heading_path="标题",
        )
        insert_segment(db, seg)
        segs = list_segments_for_document(db, doc.document_id)
        assert len(segs) == 1
        assert segs[0].segment_kind == "heading"
        assert segs[0].ordinal == 0

    def test_ordered_by_ordinal(self, db):
        from cogito.store.knowledge_repo import (
            insert_resource,
            insert_document,
            insert_segment,
            list_segments_for_document,
        )

        r = KnowledgeResource()
        insert_resource(db, r)
        doc = KnowledgeDocument(resource_id=r.resource_id)
        insert_document(db, doc)
        for i in [2, 0, 1]:
            insert_segment(db, KnowledgeSegment(document_id=doc.document_id, ordinal=i))
        segs = list_segments_for_document(db, doc.document_id)
        assert [s.ordinal for s in segs] == [0, 1, 2]


class TestKnowledgeEmbedding:
    def test_write_and_read(self, db):
        from cogito.store.knowledge_repo import (
            insert_resource,
            insert_document,
            insert_segment,
            write_embedding,
            get_embedding,
        )

        r = KnowledgeResource()
        insert_resource(db, r)
        doc = KnowledgeDocument(resource_id=r.resource_id)
        insert_document(db, doc)
        seg = KnowledgeSegment(document_id=doc.document_id)
        insert_segment(db, seg)
        vec = [0.1, 0.2, 0.3]
        write_embedding(db, seg.segment_id, vec, model="test-model", version="1")
        got = get_embedding(db, seg.segment_id, model="test-model")
        assert got is not None
        assert len(got) == 3
        assert abs(got[0] - 0.1) < 1e-6


class TestKnowledgeFTS:
    def test_fts_search(self, db):
        from cogito.store.knowledge_repo import (
            ensure_knowledge_fts,
            insert_resource,
            insert_document,
            insert_segment,
            search_knowledge_fts,
        )

        ensure_knowledge_fts(db)
        r = KnowledgeResource()
        insert_resource(db, r)
        doc = KnowledgeDocument(resource_id=r.resource_id)
        insert_document(db, doc)
        insert_segment(
            db,
            KnowledgeSegment(
                document_id=doc.document_id,
                text_ref_or_inline="Python 编程语言介绍",
                content_hash="h1",
            ),
        )
        insert_segment(
            db,
            KnowledgeSegment(
                document_id=doc.document_id,
                text_ref_or_inline="Java 开发指南",
                content_hash="h2",
            ),
        )
        results = search_knowledge_fts(db, "Python", limit=5)
        assert len(results) >= 1
        assert any("Python" in s for s in [_text_for(db, sid) for sid, _ in results])

    def test_fts_degrade_to_like(self, db):
        """FTS 不可用时降级 LIKE。"""
        from cogito.store.knowledge_repo import (
            insert_resource,
            insert_document,
            insert_segment,
            search_knowledge_fts,
        )

        r = KnowledgeResource()
        insert_resource(db, r)
        doc = KnowledgeDocument(resource_id=r.resource_id)
        insert_document(db, doc)
        insert_segment(
            db,
            KnowledgeSegment(
                document_id=doc.document_id,
                text_ref_or_inline="机器学习入门",
            ),
        )
        # 不建 FTS 表直接搜，会进 LIKE 分支
        results = search_knowledge_fts(db, "机器学习", limit=5)
        assert len(results) >= 1


def _text_for(db, segment_id):
    row = db.execute(
        "SELECT text_ref_or_inline FROM knowledge_segments WHERE segment_id=?",
        (segment_id,),
    ).fetchone()
    return row["text_ref_or_inline"] if row else ""
