"""Cogito M4~M6 原生后端（PLAN-13 P13-15）。

复用 domain/knowledge.py + store/knowledge_repo.py + service/knowledge/*.py。
"""

from __future__ import annotations

import sqlite3
import tempfile

from cogito.store.migration import migrate


class CogitoBackend:
    """Cogito 知识检索后端（基于 P13-07~09 原生实现）。"""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            self._tmp.close()
            db_path = self._tmp.name
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        migrate(self._conn)
        # 延迟导入（避免 top-level 循环）
        from cogito.service.knowledge.service import KnowledgeService

        self._svc = KnowledgeService(self._conn)

    def close(self) -> None:
        self._conn.close()

    # ── protocol ──

    def ingest(self, doc_id: str, content: str) -> list[str]:
        """摄入文档（幂等：同 doc_id 先失效再重新摄入）。"""
        # 用 doc_id 派生稳定 uri_hash
        uri_hash = f"poc://{doc_id}"
        # 是否已存在
        existing = self._conn.execute(
            "SELECT resource_id FROM knowledge_resources "
            "WHERE source_uri_hash=? AND deleted_at IS NULL",
            (uri_hash,),
        ).fetchone()
        if existing:
            self.invalidate(doc_id)
        r = self._svc.register_resource(
            source_uri_hash=uri_hash,
            source_kind="explicit_local_file",
            media_type="text/markdown",
            principal_id="poc_owner",
            content_hash=str(hash(content)),
        )
        _, segs = self._svc.ingest(r.resource_id, content)
        return [s.segment_id for s in segs]

    def retrieve(self, query: str, top_k: int = 8) -> list[tuple[str, float]]:
        return self._svc.search(query, limit=top_k)

    def invalidate(self, doc_id: str) -> None:
        uri_hash = f"poc://{doc_id}"
        row = self._conn.execute(
            "SELECT resource_id FROM knowledge_resources "
            "WHERE source_uri_hash=? AND deleted_at IS NULL",
            (uri_hash,),
        ).fetchone()
        if row:
            self._svc.erase(row["resource_id"])

    def segment_provenance(self, segment_id: str) -> str | None:
        """追溯段落地来源链。"""
        row = self._conn.execute(
            "SELECT ks.segment_id, kd.document_id, kr.source_uri_hash "
            "FROM knowledge_segments ks "
            "JOIN knowledge_documents kd ON kd.document_id = ks.document_id "
            "JOIN knowledge_resources kr ON kr.resource_id = kd.resource_id "
            "WHERE ks.segment_id=?",
            (segment_id,),
        ).fetchone()
        if not row:
            return None
        return f"{row['source_uri_hash']} > {row['document_id']} > {row['segment_id']}"
