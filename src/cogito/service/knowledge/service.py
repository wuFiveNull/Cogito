"""KnowledgeService — 内容记忆层唯一写入入口。

PLAN-13 M4 §11.3：其他模块不能直接 CRUD knowledge 表。
首版实现 register_resource + ingest（parse + segment）。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Protocol

from cogito.domain.knowledge import (
    KnowledgeDocument,
    KnowledgeResource,
    KnowledgeSegment,
    ResourceStatus,
    SegmentKind,
)
from cogito.service.knowledge.parser import (
    ContentParser,
    MarkdownParser,
    ParsedBlock,
    PlainTextParser,
)
from cogito.store import knowledge_repo

_LOGGER = logging.getLogger("cogito.knowledge")


# ── Embedding Port（P13-09 扩展）──

class EmbeddingPort(Protocol):
    """Embedding 提供者 Port。"""

    @property
    def model_id(self) -> str: ...
    @property
    def model_version(self) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class KnowledgeService:
    """内容记忆聚合唯一写入者（PLAN-13 M4）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        parser: ContentParser | None = None,
        embedder: EmbeddingPort | None = None,
    ) -> None:
        self._conn = conn
        self._parser = parser or MarkdownParser()
        self._embedder = embedder

    # ── Resource ──

    def register_resource(
        self,
        *,
        source_uri_hash: str,
        source_kind: str = "explicit_local_file",
        media_type: str = "text/markdown",
        principal_id: str = "",
        content_hash: str = "",
        trust_label: str = "unverified",
        scope_type: str = "global",
        scope_id: str = "",
        source_version: str = "",
    ) -> KnowledgeResource:
        """注册/更新知识资源。幂等：同 source_uri_hash + principal 不重复。"""
        existing = self._find_resource_by_uri(principal_id, source_uri_hash)
        if existing and existing.content_hash == content_hash:
            return existing
        r = KnowledgeResource(
            principal_id=principal_id,
            source_uri_hash=source_uri_hash,
            source_kind=source_kind,
            media_type=media_type,
            content_hash=content_hash,
            trust_label=trust_label,
            scope_type=scope_type,
            scope_id=scope_id,
            source_version=source_version,
            status=ResourceStatus.queued.value,
        )
        knowledge_repo.insert_resource(self._conn, r)
        self._conn.commit()
        _LOGGER.info("Registered knowledge resource %s", r.resource_id)
        return r

    def _find_resource_by_uri(self, principal_id: str, uri_hash: str) -> KnowledgeResource | None:
        row = self._conn.execute(
            "SELECT * FROM knowledge_resources "
            "WHERE principal_id=? AND source_uri_hash=? AND deleted_at IS NULL",
            (principal_id, uri_hash),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        return KnowledgeResource(
            resource_id=d["resource_id"], source_uri_hash=d["source_uri_hash"],
            content_hash=d.get("content_hash", ""), status=d.get("status", ""),
        )

    # ── Ingest ──

    def ingest(
        self, resource_id: str, raw_text: str,
    ) -> tuple[KnowledgeDocument, list[KnowledgeSegment]]:
        """解析 + 切分段落地（PLAN-13 M4）。"""
        r = knowledge_repo.get_resource(self._conn, resource_id)
        if r is None:
            raise ValueError(f"Resource not found: {resource_id}")
        # 解析
        blocks = self._parser.parse(raw_text)
        # 创建 document
        doc = KnowledgeDocument(
            resource_id=resource_id,
            title=self._extract_title(blocks),
            normalized_text_ref="",  # 首版 inline
            parser_id=self._parser.parser_id,
            parser_version=self._parser.parser_version,
        )
        knowledge_repo.insert_document(self._conn, doc)
        # 切分 segments
        segs = []
        for i, b in enumerate(blocks):
            seg = KnowledgeSegment(
                document_id=doc.document_id,
                ordinal=i,
                segment_kind=self._map_kind(b.kind),
                text_ref_or_inline=b.text[:2000],  # 首版内联（避免额外 payload store）
                content_hash=self._hash_text(b.text),
                token_count=max(1, len(b.text) // 4),
                heading_path=b.heading_path,
                start_offset=b.start_offset,
                end_offset=b.end_offset,
            )
            knowledge_repo.insert_segment(self._conn, seg)
            segs.append(seg)
        # 更新 resource 状态
        knowledge_repo.update_resource_status(self._conn, resource_id, ResourceStatus.active.value)
        self._conn.commit()
        _LOGGER.info(
            "Ingested resource %s → document %s, %d segments",
            resource_id, doc.document_id, len(segs),
        )
        return doc, segs

    @staticmethod
    def _extract_title(blocks: list[ParsedBlock]) -> str:
        for b in blocks:
            if b.kind == "heading":
                return b.text[:200]
        return ""

    @staticmethod
    def _map_kind(block_kind: str) -> str:
        if block_kind == "heading":
            return SegmentKind.heading.value
        return SegmentKind.paragraph.value

    @staticmethod
    def _hash_text(text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # ── Retrieval（P13-09 扩展点）──

    def search(self, query: str, limit: int = 8) -> list[tuple[str, float]]:
        """知识检索（FTS + LIKE 降级）。"""
        return knowledge_repo.search_knowledge_fts(self._conn, query, limit)

    def erase(self, resource_id: str, reason: str = "") -> int:
        """擦除资源的所有数据（PLAN-13 M4）。"""
        docs = knowledge_repo.list_documents_for_resource(self._conn, resource_id)
        count = 0
        for doc in docs:
            segs = knowledge_repo.list_segments_for_document(self._conn, doc.document_id)
            for seg in segs:
                self._conn.execute(
                    "UPDATE knowledge_segments SET deleted_at=? WHERE segment_id=?",
                    (datetime.now(UTC).isoformat(), seg.segment_id),
                )
                count += 1
        knowledge_repo.update_resource_status(self._conn, resource_id, ResourceStatus.deleted.value)
        self._conn.commit()
        return count


# ── parser 选择工具 ──

def select_parser(media_type: str) -> ContentParser:
    """按 media_type 选择解析器。"""
    if media_type in ("text/markdown", "text/x-markdown"):
        return MarkdownParser()
    return PlainTextParser()
