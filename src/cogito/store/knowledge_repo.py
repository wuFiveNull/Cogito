"""KnowledgeRepository — 内容记忆层数据访问层。

PLAN-13 P13-07：Resource/Document/Segment/Embedding CRUD、Scope、软删、索引。
其他模块不能直接 CRUD knowledge 表。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from cogito.domain.knowledge import (
    KnowledgeDocument,
    KnowledgeResource,
    KnowledgeSegment,
)

# ── Resource ──

def insert_resource(conn: sqlite3.Connection, r: KnowledgeResource) -> KnowledgeResource:
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_resources ("
        "  resource_id, principal_id, connector_id, source_uri_hash, source_kind, "
        "  media_type, payload_ref, content_hash, trust_label, scope_type, scope_id, "
        "  source_version, status, retention_class, created_at, updated_at, deleted_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            r.resource_id, r.principal_id, r.connector_id, r.source_uri_hash,
            r.source_kind, r.media_type, r.payload_ref, r.content_hash,
            r.trust_label, r.scope_type, r.scope_id, r.source_version,
            r.status, r.retention_class,
            r.created_at.isoformat(), r.updated_at.isoformat() if r.updated_at else None,
            r.deleted_at.isoformat() if r.deleted_at else None,
        ),
    )
    return r


def get_resource(conn: sqlite3.Connection, resource_id: str) -> KnowledgeResource | None:
    row = conn.execute(
        "SELECT * FROM knowledge_resources WHERE resource_id=?", (resource_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    return KnowledgeResource(
        resource_id=d["resource_id"], principal_id=d.get("principal_id", ""),
        connector_id=d.get("connector_id", ""), source_uri_hash=d.get("source_uri_hash", ""),
        source_kind=d.get("source_kind", ""), media_type=d.get("media_type", ""),
        payload_ref=d.get("payload_ref", ""), content_hash=d.get("content_hash", ""),
        trust_label=d.get("trust_label", ""), scope_type=d.get("scope_type", ""),
        scope_id=d.get("scope_id", ""), source_version=d.get("source_version", ""),
        status=d.get("status", ""), retention_class=d.get("retention_class", ""),
    )


def update_resource_status(conn: sqlite3.Connection, resource_id: str, status: str) -> None:
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
            doc.document_id, doc.resource_id, doc.title, doc.normalized_text_ref,
            doc.summary, doc.language, doc.parser_id, doc.parser_version,
            doc.content_version, doc.status,
            doc.created_at.isoformat(), doc.updated_at.isoformat() if doc.updated_at else None,
        ),
    )
    return doc


def list_documents_for_resource(
    conn: sqlite3.Connection, resource_id: str,
) -> list[KnowledgeDocument]:
    rows = conn.execute(
        "SELECT * FROM knowledge_documents WHERE resource_id=? AND status='active'",
        (resource_id,),
    ).fetchall()
    return [_row_to_document(dict(r)) for r in rows]


def _row_to_document(d: dict[str, Any]) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=d["document_id"], resource_id=d.get("resource_id", ""),
        title=d.get("title", ""), normalized_text_ref=d.get("normalized_text_ref", ""),
        summary=d.get("summary", ""), language=d.get("language", "zh"),
        parser_id=d.get("parser_id", ""), parser_version=d.get("parser_version", ""),
        content_version=d.get("content_version", ""), status=d.get("status", ""),
    )


# ── Segment ──

def insert_segment(conn: sqlite3.Connection, seg: KnowledgeSegment) -> KnowledgeSegment:
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_segments ("
        "  segment_id, document_id, ordinal, segment_kind, text_ref_or_inline, "
        "  content_hash, token_count, heading_path, start_offset, end_offset, "
        "  embedding_status, created_at, updated_at, deleted_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            seg.segment_id, seg.document_id, seg.ordinal, seg.segment_kind,
            seg.text_ref_or_inline, seg.content_hash, seg.token_count,
            seg.heading_path, seg.start_offset, seg.end_offset,
            seg.embedding_status, seg.created_at.isoformat(),
            seg.updated_at.isoformat() if seg.updated_at else None,
            seg.deleted_at.isoformat() if seg.deleted_at else None,
        ),
    )
    return seg


def list_segments_for_document(
    conn: sqlite3.Connection, document_id: str,
) -> list[KnowledgeSegment]:
    rows = conn.execute(
        "SELECT * FROM knowledge_segments "
        "WHERE document_id=? AND deleted_at IS NULL "
        "ORDER BY ordinal ASC",
        (document_id,),
    ).fetchall()
    return [_row_to_segment(dict(r)) for r in rows]


def list_unembedded_segments(
    conn: sqlite3.Connection, model: str = "", limit: int = 100,
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
                "LIMIT ?", (model, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ks.segment_id FROM knowledge_segments ks "
                "LEFT JOIN knowledge_embeddings ke ON ke.segment_id=ks.segment_id "
                "WHERE ks.deleted_at IS NULL AND ke.segment_id IS NULL "
                "LIMIT ?", (limit,),
            ).fetchall()
        return [r["segment_id"] for r in rows]
    except sqlite3.OperationalError:
        return []


def _row_to_segment(d: dict[str, Any]) -> KnowledgeSegment:
    return KnowledgeSegment(
        segment_id=d["segment_id"], document_id=d.get("document_id", ""),
        ordinal=int(d.get("ordinal", 0)),
        segment_kind=d.get("segment_kind", "paragraph"),
        text_ref_or_inline=d.get("text_ref_or_inline", ""),
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


def rebuild_knowledge_fts(conn: sqlite3.Connection) -> None:
    """全量重建知识 FTS 索引（幂等，仅含未删段落地）。PLAN-13 P13-09。"""
    try:
        conn.execute("DELETE FROM knowledge_fts")
        conn.execute(
            "INSERT INTO knowledge_fts (segment_id, text) "
            "SELECT segment_id, text_ref_or_inline FROM knowledge_segments "
            "WHERE deleted_at IS NULL AND text_ref_or_inline != ''"
        )
    except sqlite3.OperationalError:
        pass


def search_knowledge_fts(
    conn: sqlite3.Connection, query: str, limit: int = 8,
) -> list[tuple[str, float]]:
    """全文检索知识段落，返回 (segment_id, score)。"""
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
            rebuild_knowledge_fts(conn)
        rows = conn.execute(
            "SELECT segment_id FROM knowledge_fts WHERE knowledge_fts MATCH ? LIMIT ?",
            (fts_expr, limit),
        ).fetchall()
        if rows:
            return [(r["segment_id"], 1.0) for r in rows]
    except sqlite3.OperationalError:
        pass
    # LIKE 降级
        like = f"%{query}%"
        try:
            rows = conn.execute(
                "SELECT segment_id FROM knowledge_segments "
                "WHERE text_ref_or_inline LIKE ? AND deleted_at IS NULL LIMIT ?",
                (like, limit),
            ).fetchall()
            return [(r["segment_id"], 0.5) for r in rows]
        except sqlite3.OperationalError:
            return []
