"""KnowledgeRepository — 内容记忆层数据访问层。

PLAN-13 P13-07：Resource/Document/Segment/Embedding CRUD、Scope、软删、索引。
其他模块不能直接 CRUD knowledge 表。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from cogito.domain.knowledge import (
    KnowledgeDocument,
    KnowledgeResource,
    KnowledgeSegment,
)
from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore

# ── 辅助 ──


def _row_as_dict(row: Any) -> dict[str, Any]:
    """兼容 sqlite3.Row 与 tuple。"""
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return dict(row)
    # tuple fallback：无法推断列名，依赖调用方不用 tuple 路径
    return {}


def _resolve_segment_text(
    segment_row: dict[str, Any],
    make_payload_store: Any = None,
) -> str:
    payload_ref = str(segment_row.get("payload_ref") or "").strip()
    if payload_ref and make_payload_store is not None:
        try:
            data = make_payload_store().get(payload_ref)
            if data is not None:
                return data.decode("utf-8", errors="replace")
        except Exception as exc:
            logging.getLogger("cogito.knowledge_repo").warning(
                "resolve payload_ref %s failed: %s",
                payload_ref,
                exc,
            )
    return str(segment_row.get("text_ref_or_inline") or "")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


# ── Resource ──


def insert_resource(conn: sqlite3.Connection, r: KnowledgeResource) -> KnowledgeResource:
    EventStore(conn).append(
        Event(
            event_type="knowledge.resource.created",
            stream_type="knowledge_resource",
            stream_id=r.resource_id,
            producer="knowledge-repository",
            event_class=EventClass.DOMAIN,
            summary=f"Knowledge resource created: {r.source_kind}",
            attributes={
                "principal_id": r.principal_id,
                "connector_id": r.connector_id,
                "source_uri_hash": r.source_uri_hash,
                "source_kind": r.source_kind,
                "media_type": r.media_type,
                "content_hash": r.content_hash,
                "trust_label": r.trust_label,
                "scope_type": r.scope_type,
                "scope_id": r.scope_id,
                "source_version": r.source_version,
                "retention_class": r.retention_class,
            },
            outcome=r.status,
            idempotency_key=f"knowledge:resource:{r.resource_id}:created",
        ),
        expected_version=0,
    )
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_resources ("
        "  resource_id, principal_id, connector_id, source_uri_hash, source_kind, "
        "  media_type, payload_ref, content_hash, trust_label, scope_type, scope_id, "
        "  source_version, status, retention_class, created_at, updated_at, deleted_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            r.resource_id,
            r.principal_id,
            r.connector_id,
            r.source_uri_hash,
            r.source_kind,
            r.media_type,
            r.payload_ref,
            r.content_hash,
            r.trust_label,
            r.scope_type,
            r.scope_id,
            r.source_version,
            r.status,
            r.retention_class,
            r.created_at.isoformat(),
            r.updated_at.isoformat() if r.updated_at else None,
            r.deleted_at.isoformat() if r.deleted_at else None,
        ),
    )
    return r


def get_resource(conn: sqlite3.Connection, resource_id: str) -> KnowledgeResource | None:
    """Read resource from Event stream."""
    from cogito.store.event_replay import replay_knowledge_resource

    events = EventStore(conn).read_stream("knowledge_resource", resource_id)
    if not events:
        return None
    projection = replay_knowledge_resource(events, resource_id)
    if projection is None:
        return None
    attrs = events[0].attributes if events else {}
    return KnowledgeResource(
        resource_id=projection.resource_id,
        principal_id=str(attrs.get("principal_id", "")),
        connector_id=str(attrs.get("connector_id", "")),
        source_uri_hash=str(attrs.get("source_uri_hash", "")),
        source_kind=str(attrs.get("source_kind", "")),
        media_type=str(attrs.get("media_type", "")),
        payload_ref=str(attrs.get("payload_ref", "")),
        content_hash=str(attrs.get("content_hash", "")),
        trust_label=str(attrs.get("trust_label", "")),
        scope_type=str(attrs.get("scope_type", "")),
        scope_id=str(attrs.get("scope_id", "")),
        source_version=str(attrs.get("source_version", "")),
        status=projection.status,
        retention_class=str(attrs.get("retention_class", "")),
    )


def update_resource_status(conn: sqlite3.Connection, resource_id: str, status: str) -> None:
    EventStore(conn).append(
        Event(
            event_type="knowledge.resource.updated",
            stream_type="knowledge_resource",
            stream_id=resource_id,
            producer="knowledge-repository",
            event_class=EventClass.DOMAIN,
            summary=f"Knowledge resource status: {status}",
            attributes={"status": status},
            outcome=status,
            idempotency_key=f"knowledge:resource:{resource_id}:status:{status}",
        ),
    )
    conn.execute(
        "UPDATE knowledge_resources SET status=?, updated_at=? WHERE resource_id=?",
        (status, datetime.now(UTC).isoformat(), resource_id),
    )


# ── Document ──


def insert_document(conn: sqlite3.Connection, doc: KnowledgeDocument) -> KnowledgeDocument:
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_documents ("
        "  document_id, resource_id, title, normalized_text_ref, summary, language, "
        "  parser_id, parser_version, content_version, status, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            doc.document_id,
            doc.resource_id,
            doc.title,
            doc.normalized_text_ref,
            doc.summary,
            doc.language,
            doc.parser_id,
            doc.parser_version,
            doc.content_version,
            doc.status,
            doc.created_at.isoformat(),
            doc.updated_at.isoformat() if doc.updated_at else None,
        ),
    )
    return doc


def list_documents_for_resource(
    conn: sqlite3.Connection,
    resource_id: str,
) -> list[KnowledgeDocument]:
    rows = conn.execute(
        "SELECT * FROM knowledge_documents WHERE resource_id=? AND status='active'",
        (resource_id,),
    ).fetchall()
    return [_row_to_document(dict(r)) for r in rows]


def _row_to_document(d: dict[str, Any]) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=d["document_id"],
        resource_id=d.get("resource_id", ""),
        title=d.get("title", ""),
        normalized_text_ref=d.get("normalized_text_ref", ""),
        summary=d.get("summary", ""),
        language=d.get("language", "zh"),
        parser_id=d.get("parser_id", ""),
        parser_version=d.get("parser_version", ""),
        content_version=d.get("content_version", ""),
        status=d.get("status", ""),
    )


# ── Segment ──


def insert_segment(conn: sqlite3.Connection, seg: KnowledgeSegment) -> KnowledgeSegment:
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_segments ("
        "  segment_id, document_id, ordinal, segment_kind, text_ref_or_inline, "
        "  payload_ref, content_hash, token_count, heading_path, start_offset, "
        "  end_offset, embedding_status, created_at, updated_at, deleted_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            seg.segment_id,
            seg.document_id,
            seg.ordinal,
            seg.segment_kind,
            seg.text_ref_or_inline,
            seg.payload_ref,
            seg.content_hash,
            seg.token_count,
            seg.heading_path,
            seg.start_offset,
            seg.end_offset,
            seg.embedding_status,
            seg.created_at.isoformat(),
            seg.updated_at.isoformat() if seg.updated_at else None,
            seg.deleted_at.isoformat() if seg.deleted_at else None,
        ),
    )
    return seg


def list_segments_for_document(
    conn: sqlite3.Connection,
    document_id: str,
) -> list[KnowledgeSegment]:
    rows = conn.execute(
        "SELECT * FROM knowledge_segments "
        "WHERE document_id=? AND deleted_at IS NULL "
        "ORDER BY ordinal ASC",
        (document_id,),
    ).fetchall()
    return [_row_to_segment(dict(r)) for r in rows]


def list_unembedded_segments(
    conn: sqlite3.Connection,
    model: str = "",
    limit: int = 100,
) -> list[str]:
    """列出尚未为当前模型生成 Embedding 的段落地 ID。"""
    try:
        if model:
            rows = conn.execute(
                "SELECT ks.segment_id FROM knowledge_segments ks "
                "LEFT JOIN knowledge_embeddings ke "
                "  ON ke.segment_id=ks.segment_id AND ke.embedding_model=? "
                "WHERE ks.deleted_at IS NULL AND ks.embedding_status!='ready' "
                "AND ke.segment_id IS NULL "
                "LIMIT ?",
                (model, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ks.segment_id FROM knowledge_segments ks "
                "LEFT JOIN knowledge_embeddings ke ON ke.segment_id=ks.segment_id "
                "WHERE ks.deleted_at IS NULL AND ke.segment_id IS NULL "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["segment_id"] for r in rows]
    except sqlite3.OperationalError:
        return []


def _row_to_segment(d: dict[str, Any]) -> KnowledgeSegment:
    return KnowledgeSegment(
        segment_id=d["segment_id"],
        document_id=d.get("document_id", ""),
        ordinal=int(d.get("ordinal", 0)),
        segment_kind=d.get("segment_kind", "paragraph"),
        text_ref_or_inline=d.get("text_ref_or_inline", ""),
        payload_ref=d.get("payload_ref", ""),
        content_hash=d.get("content_hash", ""),
        token_count=int(d.get("token_count", 0)),
        heading_path=d.get("heading_path", ""),
        start_offset=int(d.get("start_offset", 0)),
        end_offset=int(d.get("end_offset", 0)),
        embedding_status=d.get("embedding_status", "pending"),
    )


def write_embedding(
    conn: sqlite3.Connection,
    segment_id: str,
    vector: list[float],
    model: str = "",
    version: str = "",
) -> None:
    if not vector:
        return
    import json

    blob = json.dumps(vector).encode("utf-8")
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_embeddings "
        "(segment_id, embedding_model, embedding_version, vector, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (segment_id, model, version, blob, datetime.now(UTC).isoformat()),
    )


def get_embedding(conn: sqlite3.Connection, segment_id: str, model: str = "") -> list[float] | None:
    import json

    try:
        sql = "SELECT vector FROM knowledge_embeddings WHERE segment_id=?"
        params: list[Any] = [segment_id]
        if model:
            sql += " AND embedding_model=?"
            params.append(model)
        row = conn.execute(sql, params).fetchone()
        if row and row["vector"]:
            return json.loads(row["vector"])
    except (sqlite3.OperationalError, Exception):
        pass
    return None


# ── FTS5 知识全文索引 ──


def ensure_knowledge_fts(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts "
            "USING fts5(segment_id UNINDEXED, text, tokenize='unicode61')"
        )
        rebuild_knowledge_fts(conn)
        return True
    except sqlite3.OperationalError:
        return False


def rebuild_knowledge_fts(conn: sqlite3.Connection, make_payload_store=None) -> None:
    """全量重建知识 FTS 索引（幂等，仅含未删段落地）。PLAN-13 P13-09 + PLAN-16 完整。"""
    try:
        conn.execute("DELETE FROM knowledge_fts")
        rows = conn.execute("SELECT * FROM knowledge_segments WHERE deleted_at IS NULL").fetchall()
        for r in rows:
            text = _resolve_segment_text(dict(r), make_payload_store)
            if text:
                conn.execute(
                    "INSERT INTO knowledge_fts (segment_id, text) VALUES (?, ?)",
                    (r["segment_id"], text),
                )
    except sqlite3.OperationalError:
        pass


def purge_segments_for_resource(
    conn: sqlite3.Connection,
    resource_id: str,
) -> int:
    """擦除资源的所有段落地（PLAN-16 M5 KNOW-08）。

    清空 segment 正文（最小 tombstone）、删除 embedding 行、标记 deleted_at，
    并重建 FTS 使被擦除正文/vector 不再保留。返回被清理的段数。
    """
    now = datetime.now(UTC).isoformat()
    docs = list_documents_for_resource(conn, resource_id)
    count = 0
    try:
        seg_ids = []
        for doc in docs:
            for seg in list_segments_for_document(conn, doc.document_id):
                seg_ids.append(seg.segment_id)
        for sid in seg_ids:
            conn.execute(
                "UPDATE knowledge_segments "
                "SET text_ref_or_inline='', deleted_at=?, embedding_status='pending' "
                "WHERE segment_id=? AND deleted_at IS NULL",
                (now, sid),
            )
            conn.execute(
                "DELETE FROM knowledge_embeddings WHERE segment_id=?",
                (sid,),
            )
            count += 1
        ensure_knowledge_fts(conn)
    except sqlite3.OperationalError as e:
        logging.getLogger("cogito.knowledge_repo").warning(
            "purge_segments_for_resource partial failure: %s", e
        )
    return count


def search_knowledge_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 8,
    make_payload_store=None,
) -> list[tuple[str, float]]:
    """全文检索知识段落，返回 (segment_id, score)。

    PLAN-16 完整：make_payload_store 提供时 resolver 化 payload 段落。
    """
    import re

    if not query:
        return []
    tokens = re.findall(r"[-\w一-鿿鿿㐀-䶿]+", query, re.UNICODE)
    if not tokens:
        return []
    fts_expr = " OR ".join(tokens) if len(tokens) > 1 else tokens[0]
    try:
        # 延迟同步：如果 FTS 表为空但 segment 有数据，先重建
        cnt = conn.execute("SELECT COUNT(*) c FROM knowledge_fts").fetchone()["c"]
        if cnt == 0:
            rebuild_knowledge_fts(conn, make_payload_store=make_payload_store)
        rows = conn.execute(
            "SELECT f.segment_id FROM knowledge_fts AS f "
            "JOIN knowledge_segments AS s ON s.segment_id=f.segment_id "
            "JOIN knowledge_documents AS d ON d.document_id=s.document_id "
            "JOIN knowledge_resources AS r ON r.resource_id=d.resource_id "
            "WHERE knowledge_fts MATCH ? "
            "AND s.deleted_at IS NULL AND d.status='active' "
            "AND r.status='active' AND r.deleted_at IS NULL LIMIT ?",
            (fts_expr, limit),
        ).fetchall()
        if rows:
            return [(r["segment_id"], 1.0) for r in rows]
    except sqlite3.OperationalError:
        pass
    # LIKE 降级（PLAN-16 完整：resolver 化 payload 段落后匹配）
    like = f"%{query}%"
    try:
        candidates = conn.execute(
            "SELECT * FROM knowledge_segments WHERE deleted_at IS NULL",
        ).fetchall()
        out = []
        for r in candidates:
            text = _resolve_segment_text(dict(r), make_payload_store)
            if text and like.strip("%") in text:
                out.append((r["segment_id"], 0.5))
            if len(out) >= limit:
                break
        return out
    except sqlite3.OperationalError:
        return []


def search_knowledge_vector(
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    principal_id: str = "",
    model: str = "",
    limit: int = 8,
) -> list[tuple[str, float]]:
    """Brute-force cosine recall over active, authorized knowledge segments."""
    if not query_vector:
        return []
    conditions = ["ks.deleted_at IS NULL", "kr.deleted_at IS NULL", "kr.status='active'"]
    params: list[Any] = []
    if principal_id:
        conditions.append("kr.principal_id=?")
        params.append(principal_id)
    if model:
        conditions.append("ke.embedding_model=?")
        params.append(model)
    rows = conn.execute(
        "SELECT ks.segment_id, ke.vector FROM knowledge_segments ks "
        "JOIN knowledge_documents kd ON kd.document_id=ks.document_id "
        "JOIN knowledge_resources kr ON kr.resource_id=kd.resource_id "
        "JOIN knowledge_embeddings ke ON ke.segment_id=ks.segment_id "
        "WHERE " + " AND ".join(conditions),
        params,
    ).fetchall()
    scored: list[tuple[str, float]] = []
    for row in rows:
        raw = row["vector"]
        try:
            vector = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            continue
        score = _cosine_similarity(query_vector, vector)
        if score > 0:
            scored.append((row["segment_id"], score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def get_segment_context(conn: sqlite3.Connection, segment_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT ks.*, kd.title, kd.resource_id, kr.principal_id, kr.scope_type, "
        "kr.scope_id, kr.trust_label, kr.source_version, kr.source_kind "
        "FROM knowledge_segments ks "
        "JOIN knowledge_documents kd ON kd.document_id=ks.document_id "
        "JOIN knowledge_resources kr ON kr.resource_id=kd.resource_id "
        "WHERE ks.segment_id=? AND ks.deleted_at IS NULL AND kd.status='active' "
        "AND kr.status='active' AND kr.deleted_at IS NULL",
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None
