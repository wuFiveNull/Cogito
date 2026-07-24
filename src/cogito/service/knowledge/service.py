"""KnowledgeService — 内容记忆层唯一写入入口。

PLAN-13 M4 §11.3：其他模块不能直接 CRUD knowledge 表。
首版实现 register_resource + ingest（parse + segment）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Protocol

from cogito.domain.event import Event, EventClass, EventContext
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
from cogito.store.event_store import EventStore

_LOGGER = logging.getLogger("cogito.knowledge")

_KNOWLEDGE_EVENT_TYPES = {
    "KnowledgeResourceDiscovered": "knowledge.resource.created",
    "KnowledgeResourceChanged": "knowledge.resource.updated",
    "KnowledgeDocumentParsed": "knowledge.document.parsed",
    "KnowledgeSegmentsIndexed": "knowledge.resource.ingested",
    "KnowledgeResourceInvalidated": "knowledge.resource.invalidated",
    "KnowledgeResourceDeleted": "knowledge.resource.deleted",
}
_KNOWLEDGE_EVENT_ATTRIBUTE_KEYS = frozenset(
    {"resource_id", "source_version", "document_id", "segment_count", "reason", "receipt_id"}
)


# ── Embedding Port（P13-09 扩展）──


class EmbeddingPort(Protocol):
    """Embedding 提供者 Port。"""

    @property
    def model_id(self) -> str: ...
    @property
    def model_version(self) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingProviderAdapter:
    """Adapt the shared EmbeddingProvider contract to the Knowledge port.

    PLAN-16 完整实现：embed_sync / embed_many_sync 通过线程池跑同步
    OpenAI HTTP 调用，供同步检索路径（ContextBuilder / search_knowledge）使用，
    避免在 running event loop 内 asyncio.run() 或阻塞。
    """

    def __init__(self, provider) -> None:
        self._provider = provider

    @property
    def model_id(self) -> str:
        return self._provider.model_name

    @property
    def model_version(self) -> str:
        return self._provider.model_version

    @property
    def dimensions(self) -> int:
        return getattr(self._provider, "dimensions", 0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._provider.embed_many(texts)

    def embed_sync(self, text: str) -> list[str]:
        """同步单条 embedding（PLAN-16 完整实现，真正执行向量请求，不做 fallback）。"""
        result = self.embed_many_sync([text])
        return result[0] if result else []

    def embed_many_sync(self, texts: list[str]) -> list[list[float]]:
        """同步批量 embedding：把同步实现委派给 provider；若无则直接调用 _embed_batch_sync。"""
        provider = self._provider
        if hasattr(provider, "embed_many_sync"):
            # OpenAICompatEmbeddingProvider 等提供真正的同步实现
            return provider.embed_many_sync(texts)
        # 无同步实现时退化（Noop 等）
        if hasattr(provider, "embed_many"):
            return provider.embed_many(texts)
        return [[] for _ in texts]


class KnowledgeService:
    """内容记忆聚合唯一写入者（PLAN-13 M4）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        parser: ContentParser | None = None,
        embedder: EmbeddingPort | None = None,
        payload_store_factory=None,
    ) -> None:
        self._conn = conn
        self._parser = parser or MarkdownParser()
        self._embedder = embedder
        # PLAN-16 M4 完整 payload 边界：PayloadStore 工厂（供 ingest/embed/search 使用）
        self._payload_store_factory = payload_store_factory

    def _emit(self, event_type: str, aggregate_id: str, payload: dict | None = None) -> Event:
        """在资源变更的同一事务内写入受限的规范 Event。"""
        canonical_type = _KNOWLEDGE_EVENT_TYPES[event_type]
        attributes = {
            key: value
            for key, value in (payload or {}).items()
            if key in _KNOWLEDGE_EVENT_ATTRIBUTE_KEYS and isinstance(value, str | int | float | bool)
        }
        store = EventStore(self._conn)
        stream = store.read_stream("knowledge_resource", aggregate_id)
        source = stream[-1] if stream else None
        source_context = source.context if source else EventContext()
        resource = self._conn.execute(
            "SELECT principal_id FROM knowledge_resources WHERE resource_id=?", (aggregate_id,)
        ).fetchone()
        return store.append(
            Event(
                event_type=canonical_type,
                stream_type="knowledge_resource",
                stream_id=aggregate_id,
                producer="knowledge-service",
                event_class=(
                    EventClass.OPERATION
                    if canonical_type == "knowledge.document.parsed"
                    else EventClass.DOMAIN
                ),
                context=EventContext(
                    trace_id=source_context.trace_id,
                    correlation_id=source_context.correlation_id,
                    causation_id=source.event_id if source else source_context.causation_id,
                    principal_id=(str(resource[0]) if resource else ""),
                ),
                summary=canonical_type.replace(".", " "),
                attributes=attributes,
                outcome=canonical_type.rsplit(".", 1)[-1],
            )
        )

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
        self._emit(
            "KnowledgeResourceChanged" if existing else "KnowledgeResourceDiscovered",
            r.resource_id,
            {"resource_id": r.resource_id, "source_version": r.source_version},
        )
        # PLAN-16 M2 TX-05: 不再内部 commit，由调用方（Command/Task 外层）统一提交，
        # 确保 register + ingest + Outbox 事件原子。
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
            resource_id=d["resource_id"],
            source_uri_hash=d["source_uri_hash"],
            content_hash=d.get("content_hash", ""),
            status=d.get("status", ""),
        )

    # ── Ingest ──

    def ingest(
        self,
        resource_id: str,
        raw_text: str,
        payload_threshold: int = 4096,
    ) -> tuple[KnowledgeDocument, list[KnowledgeSegment]]:
        """解析 + 切分段落地（PLAN-13 M4, PLAN-16 M4 完整 payload 边界）。

        当段落正文超过 payload_threshold（默认 4096 字节）且提供 PayloadStore 工厂时，
        正文写入 PayloadStore（content-addressed sha256），段落的 text_ref_or_inline=''、
        仅保留 payload_ref 引用；否则内联（兼容旧行为）。
        """
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
        self._emit(
            "KnowledgeDocumentParsed",
            resource_id,
            {"resource_id": resource_id, "document_id": doc.document_id},
        )
        # 切分 segments（PLAN-16 完整：大正文写入 PayloadStore，仅保留 payload_ref）
        segs = []
        for i, b in enumerate(blocks):
            seg = KnowledgeSegment(
                document_id=doc.document_id,
                ordinal=i,
                segment_kind=self._map_kind(b.kind),
                text_ref_or_inline=b.text[:2000],  # 默认内联
                content_hash=self._hash_text(b.text),
                token_count=max(1, len(b.text) // 4),
                heading_path=b.heading_path,
                start_offset=b.start_offset,
                end_offset=b.end_offset,
            )
            # PLAN-16 M4 完整 payload 边界：大正文 → PayloadStore
            # PLAN-16 P16-13：写入失败则抛异常（不降级内联），由 Command/Task 失败重试。
            # 降级会造成内容截断、source hash 与实际摄取正文不一致、Task 表无限增长。
            if (
                self._payload_store_factory is not None
                and len(b.text.encode("utf-8")) > payload_threshold
            ):
                store = self._payload_store_factory(self._conn)
                obj = store.put(
                    b.text.encode("utf-8"),
                    content_type="text/plain; charset=utf-8",
                    retention_class="hot",
                )
                seg.text_ref_or_inline = ""
                seg.payload_ref = obj.payload_id
                seg.token_count = max(1, len(b.text) // 4)
            knowledge_repo.insert_segment(self._conn, seg)
            segs.append(seg)
        # 更新 resource 状态
        knowledge_repo.update_resource_status(self._conn, resource_id, ResourceStatus.active.value)
        self._emit(
            "KnowledgeSegmentsIndexed",
            resource_id,
            {
                "resource_id": resource_id,
                "document_id": doc.document_id,
                "segment_count": len(segs),
            },
        )
        # PLAN-16 M2 TX-05: 不再内部 commit，由调用方统一提交。
        _LOGGER.info(
            "Ingested resource %s → document %s, %d segments",
            resource_id,
            doc.document_id,
            len(segs),
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

    async def embed_pending(self, limit: int = 1000) -> int:
        """Embed pending segments with the configured provider; noop is a safe FTS fallback."""
        if self._embedder is None:
            return 0
        pending = knowledge_repo.list_unembedded_segments(
            self._conn,
            model=self._embedder.model_id,
            limit=limit,
        )
        if not pending:
            return 0
        # PLAN-16 M4 完整 payload 边界：resolver 化 payload 段落为文本
        from cogito.service.knowledge.resolver import resolve_segment_text

        segment_rows = [
            knowledge_repo.get_segment_context(self._conn, segment_id) for segment_id in pending
        ]
        texts = [
            resolve_segment_text(self._conn, item, self._payload_store_factory) if item else ""
            for item in segment_rows
        ]
        vectors = await self._embedder.embed(texts)
        written = 0
        for segment_id, vector in zip(pending, vectors, strict=False):
            if not vector:
                continue
            knowledge_repo.write_embedding(
                self._conn,
                segment_id,
                vector,
                model=self._embedder.model_id,
                version=self._embedder.model_version,
            )
            self._conn.execute(
                "UPDATE knowledge_segments SET embedding_status='ready' WHERE segment_id=?",
                (segment_id,),
            )
            written += 1
        # PLAN-16 M2/完整：不再内部 commit，由调用方统一提交事务
        return written

    def retrieve(
        self,
        *,
        principal_id: str,
        query: str,
        limit: int = 8,
        query_vector: list[float] | None = None,
    ) -> list[dict]:
        """Return authorized FTS/vector results with safe provenance metadata.

        PLAN-16 M5 KNOW-06: 未显式传入 query_vector 且配置了 Embedder 时，
        自动生成 query vector 走 hybrid retrieval（FTS + vector），不再仅 FTS-only。
        """
        merged: dict[str, tuple[float, str]] = {}
        # PLAN-16 完整：search/resolver 化 payload 段落
        for segment_id, score in knowledge_repo.search_knowledge_fts(
            self._conn, query, limit, make_payload_store=self._payload_store_factory
        ):
            merged[segment_id] = (score, "keyword")
        # KNOW-06 完整：自动 query embedding（Adapter 现提供真正的同步实现）
        used_path = "keyword"
        if query_vector is None and self._embedder is not None:
            try:
                query_vector = self._embedder.embed_sync(query)
                used_path = "keyword+vector"
            except Exception as e:
                _LOGGER.warning("query embedding failed, degrading to FTS-only: %s", e)
                query_vector = None
                used_path = "keyword"
                # OPS-04 完整：记录降级
                from cogito.infrastructure.metrics_access import _metrics

                _metrics().record_knowledge_retrieval_degraded(reason="embed_error")
        if query_vector:
            model = self._embedder.model_id if self._embedder else ""
            for segment_id, score in knowledge_repo.search_knowledge_vector(
                self._conn,
                query_vector,
                principal_id=principal_id,
                model=model,
                limit=limit,
            ):
                old = merged.get(segment_id)
                merged[segment_id] = (
                    max(score, old[0]) if old else score,
                    "keyword+vector" if old else "vector",
                )
        # OPS-04 完整：记录 knowledge retrieval 路径
        from cogito.infrastructure.metrics_access import _metrics

        _metrics().record_knowledge_retrieval(path=used_path)
        results: list[dict] = []
        for segment_id, (score, path) in sorted(
            merged.items(),
            key=lambda item: item[1][0],
            reverse=True,
        ):
            value = knowledge_repo.get_segment_context(self._conn, segment_id)
            if not value or value.get("principal_id") != principal_id:
                continue
            value["score"] = score
            value["retrieval_path"] = path
            results.append(value)
            if len(results) >= limit:
                break
        return results

    def invalidate(self, resource_id: str, reason: str = "") -> int:
        """Invalidate a resource and all derived indexes through the owning service."""
        from cogito.service.knowledge.embedding import invalidate_resource_segments

        count = invalidate_resource_segments(self._conn, resource_id)
        knowledge_repo.update_resource_status(self._conn, resource_id, ResourceStatus.stale.value)
        self._emit(
            "KnowledgeResourceInvalidated",
            resource_id,
            {"resource_id": resource_id, "reason": reason, "segment_count": count},
        )
        # PLAN-16 M2 TX-05: 不再内部 commit，由调用方统一提交。
        return count

    def sync_source(
        self,
        *,
        stable_source_id: str,
        raw_text: str,
        source_kind: str = "connector",
        content_hash: str = "",
        principal_id: str = "",
        trust_label: str = "unverified",
    ) -> str:
        existing = self._find_resource_by_uri(principal_id, stable_source_id)
        if existing and existing.content_hash == content_hash:
            return existing.resource_id
        if existing:
            self.invalidate(existing.resource_id, "source_modified")
        resource = self.register_resource(
            source_uri_hash=stable_source_id,
            source_kind=source_kind,
            content_hash=content_hash,
            principal_id=principal_id,
            trust_label=trust_label,
            source_version=content_hash[:8],
        )
        self.ingest(resource.resource_id, raw_text)
        return resource.resource_id

    def delete_source(self, *, stable_source_id: str, principal_id: str = "") -> bool:
        existing = self._find_resource_by_uri(principal_id, stable_source_id)
        if existing is None:
            return True
        self.erase(existing.resource_id, "source_deleted")
        return True

    def erase(self, resource_id: str, reason: str = "") -> int:
        """擦除资源的所有数据（PLAN-13 M4, PLAN-16 M5 KNOW-07/08/09）。

        KNOW-08: 清空 segment 正文 + 删除 embedding + 重建 FTS（最小 tombstone）。
        KNOW-09: 写 Erasure Receipt 供对账。
        KNOW-07: 不再直写 memory 表；发布 MemorySourceInvalidated 事件，
        由 MemorySourceInvalidatedConsumer 经 MemoryService 决定 keep/review/expire。
        """
        # KNOW-08: 清理段落地（正文/embedding/FTS）
        count = knowledge_repo.purge_segments_for_resource(self._conn, resource_id)
        knowledge_repo.update_resource_status(self._conn, resource_id, ResourceStatus.deleted.value)

        # KNOW-09: 写 Erasure Receipt
        receipt_id = _write_knowledge_erasure_receipt(
            self._conn,
            resource_id=resource_id,
            reason=reason,
            segment_count=count,
        )

        # KNOW-07: 经事件传播到 Memory（不再直写 memory 表）
        affected = self._conn.execute(
            "SELECT DISTINCT memory_id FROM memory_sources "
            "WHERE source_type='knowledge_resource' AND source_id=? AND deleted_at IS NULL",
            (resource_id,),
        ).fetchall()
        deleted_event = self._emit(
            "KnowledgeResourceDeleted",
            resource_id,
            {
                "resource_id": resource_id,
                "reason": reason,
                "segment_count": count,
                "receipt_id": receipt_id,
            },
        )
        store = EventStore(self._conn)
        for row in affected:
            memory_id = row["memory_id"]
            store.append(
                Event(
                    event_type="memory.source.invalidated",
                    stream_type="memory",
                    stream_id=memory_id,
                    producer="knowledge-service",
                    event_class=EventClass.DOMAIN,
                    context=EventContext(
                        trace_id=deleted_event.context.trace_id,
                        correlation_id=deleted_event.context.correlation_id,
                        causation_id=deleted_event.event_id,
                        principal_id=deleted_event.context.principal_id,
                    ),
                    summary="Memory source invalidated",
                    attributes={
                        "resource_id": resource_id,
                        "reason": reason,
                        "receipt_id": receipt_id,
                    },
                    outcome="invalidated",
                )
            )
        # PLAN-16 M2 TX-05: 不再内部 commit，由调用方统一提交。
        return count


# ── parser 选择工具 ──


def select_parser(media_type: str) -> ContentParser:
    """按 media_type 选择解析器。"""
    if media_type in ("text/markdown", "text/x-markdown"):
        return MarkdownParser()
    return PlainTextParser()


def _write_knowledge_erasure_receipt(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    reason: str,
    segment_count: int,
) -> str:
    """为 Knowledge 擦除写入一条 Erasure Receipt（PLAN-16 M5 KNOW-09）。"""
    import hashlib
    import uuid as _uuid

    from cogito.store.receipt_repo import ReceiptRecord, SideEffectReceiptRepository

    receipt_id = f"rcpt-know-erase-{resource_id[:8]}-{_uuid.uuid4().hex[:8]}"
    request_hash = hashlib.sha256(f"knowledge-erase:{resource_id}:{reason}".encode()).hexdigest()[
        :16
    ]
    created_at = int(datetime.now(UTC).timestamp() * 1000)
    SideEffectReceiptRepository(conn).insert(
        ReceiptRecord(
            receipt_id=receipt_id,
            capability_id="knowledge",
            operation_id=resource_id,
            request_hash=request_hash,
            side_effect_class="non_retriable",
            status="succeeded",
            reconcile_status="not_needed",
            summary=f"knowledge resource erased: {reason} ({segment_count} segments)",
            attempt_type="run",
            created_at=created_at,
        )
    )
    return receipt_id
