"""P13-08: KnowledgeService ingest/parse/segment tests.

PLAN-13 M4：register_resource + ingest（parse + segment）。
"""

from __future__ import annotations

import sqlite3

import pytest

from cogito.service.knowledge.parser import MarkdownParser, PlainTextParser
from cogito.service.knowledge.service import KnowledgeService


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


class TestKnowledgeServiceIngest:
    def test_register_and_ingest_markdown(self, svc, db):
        r = svc.register_resource(
            source_uri_hash="hash-md-1",
            principal_id="owner",
        )
        assert r.status == "queued"
        md = "# 标题\n\n这是第一段内容。\n\n## 子标题\n\n第二段内容。"
        doc, segs = svc.ingest(r.resource_id, md)
        assert doc.title == "标题"
        assert len(segs) >= 3  # 2 headings + paragraphs
        # resource 状态推进到 active
        row = db.execute(
            "SELECT status FROM knowledge_resources WHERE resource_id=?",
            (r.resource_id,),
        ).fetchone()
        assert row["status"] == "active"

    def test_ingest_plaintext(self, db):
        svc = KnowledgeService(db, parser=PlainTextParser())
        r = svc.register_resource(source_uri_hash="hash-pt-1", principal_id="owner")
        text = "第一段。\n\n第二段内容。\n\n第三段。"
        doc, segs = svc.ingest(r.resource_id, text)
        assert len(segs) == 3

    def test_register_idempotent(self, svc):
        """同 source_uri_hash + content_hash 不重复注册。"""
        r1 = svc.register_resource(source_uri_hash="same-hash", content_hash="c1")
        r2 = svc.register_resource(source_uri_hash="same-hash", content_hash="c1")
        assert r1.resource_id == r2.resource_id

    def test_ingest_empty(self, svc):
        r = svc.register_resource(source_uri_hash="empty")
        doc, segs = svc.ingest(r.resource_id, "")
        assert len(segs) == 0

    def test_resource_traceable_to_segments(self, svc):
        """Resource → Document → Segment 可追溯（PLAN-13 M4）。"""
        r = svc.register_resource(source_uri_hash="trace-1")
        md = "# A\n\n文本 A。\n\n## B\n\n文本 B。"
        doc, segs = svc.ingest(r.resource_id, md)
        assert doc.resource_id == r.resource_id
        for s in segs:
            assert s.document_id == doc.document_id

    def test_erase(self, svc):
        r = svc.register_resource(source_uri_hash="erase-1")
        svc.ingest(r.resource_id, "# T\n\n内容。")
        count = svc.erase(r.resource_id)
        assert count >= 1


class TestMarkdownParser:
    def test_heading_split(self):
        md = "# H1\n\nPara 1.\n\n## H2\n\nPara 2."
        blocks = MarkdownParser().parse(md)
        headings = [b for b in blocks if b.kind == "heading"]
        assert len(headings) == 2
        assert headings[0].text == "H1"

    def test_heading_path(self):
        md = "# Top\n\nText.\n\n## Sub\n\nMore."
        blocks = MarkdownParser().parse(md)
        paras = [b for b in blocks if b.kind == "paragraph"]
        assert any("Top" in p.heading_path for p in paras)

    def test_empty(self):
        assert MarkdownParser().parse("") == []
