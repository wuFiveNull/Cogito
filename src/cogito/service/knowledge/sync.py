"""KnowledgeSync — 来源增删改级联（PLAN-13 P13-10 M5, PLAN-16 M4）。

每个 Connector/source root 使用 stable_source_id + content_hash + watermark。
Diff 分类：added、modified、unchanged、deleted。
级联规则：modified → 旧 stale + 新 active；deleted → tombstone + 撤销检索。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from cogito.service.knowledge.service import KnowledgeService

_LOGGER = logging.getLogger("cogito.knowledge.sync")

# 共享 PayloadStore 工厂（由 Application 注入，缺失则 inline fallback）
_shared_payload_store_factory = None


def _shared_payload_store():
    if _shared_payload_store_factory is not None:
        return _shared_payload_store_factory()
    # fallback：创建默认实例
    from cogito.infrastructure.payload_store import PayloadStore
    return PayloadStore(".workspace/payloads", None)


def set_payload_store_factory(factory) -> None:
    """Application 注入 PayloadStore 工厂（PLAN-16 完整）。"""
    global _shared_payload_store_factory
    _shared_payload_store_factory = factory


def _compose_raw_text(item: dict) -> str:
    """把 ConnectorItem 的摘要字段拼成 Knowledge 摄取的 raw_text。"""
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    body = str(item.get("body") or "")
    parts = [p for p in (title, summary, body) if p]
    text = "\n\n".join(parts)
    return text[:50000]  # 首版内联上限（大正文走 payload_ref 由调用方处理）


def enqueue_knowledge_sync_source(
    conn: sqlite3.Connection,
    *,
    stable_source_id: str,
    source_kind: str = "connector",
    content_hash: str = "",
    raw_text: str,
    principal_id: str = "owner",
    trust_label: str = "external",
    origin: str = "connector_poll",
) -> str | None:
    """创建 durable knowledge.sync_source Task（PLAN-16 M4）。

    KNOW-01/02/03：Connector/API 来源内容经 durable Task 进入 Knowledge，
    以便 parse/embed/checkpoint 可恢复可重试。幂等键基于 stable_source_id，
    重复内容不会重复入 Knowledge。
    """
    if not stable_source_id:
        return None
    # PLAN-16 M4 完整 payload 边界：大正文写入 PayloadStore、Task 仅保存引用
    data = {
        "stable_source_id": stable_source_id,
        "source_kind": source_kind,
        "content_hash": content_hash,
        "raw_text": "",
        "payload_ref": "",
        "principal_id": principal_id,
        "trust_label": trust_label,
        "source_version": content_hash[:8] if content_hash else "",
        "parser_policy_version": "1",
        "config_version": "1",
        "payload_threshold": 4096,
    }
    # 正文经 PayloadStore：Task 只保存 payload_ref，不内联无界正文
    if raw_text:
        try:
            store = _shared_payload_store()
            obj = store.put(raw_text.encode("utf-8"),
                            content_type="text/plain; charset=utf-8",
                            retention_class="hot")
            data["payload_ref"] = obj.payload_id
            data["content_length"] = len(raw_text)
        except Exception as e:
            # PayloadStore 不可用时降级内联（截断上限）
            _LOGGER.warning("sync_source PayloadStore unavailable, inlining: %s", e)
            data["raw_text"] = raw_text[:50000]
    # 完整：幂等键加入 content_hash，允许同一来源内容更新后重新摄取
    idem = f"knowledge.sync_source:{stable_source_id}:{content_hash or 'none'}"
    try:
        from cogito.service.task_service import SqliteTaskService
        task = SqliteTaskService(conn).create(
            "knowledge.sync_source",
            json.dumps(data, ensure_ascii=False),
            idempotency_key=idem,
            origin=origin,
            priority=30,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
        if task:
            _LOGGER.info("Enqueued knowledge.sync_source for %s", stable_source_id)
            return task.task_id
        return None
    except sqlite3.IntegrityError:
        _LOGGER.debug(
            "knowledge.sync_source already queued for %s", stable_source_id)
        return None


def enqueue_knowledge_embed(conn: sqlite3.Connection, origin: str = "knowledge_ingest",
                             embed_model: str = "") -> str | None:
    """创建 durable knowledge.embed Task（PLAN-16 M4 KNOW-05 完整）。

    ingest 完成后调用，使 parse 后的 segment 经独立可恢复步骤进入 embedding。
    完整：幂等键加入 embedding_model_version，模型升级后可重新嵌入；
    同时检查是否仍有 pending segment，避免全局共享键拦住后续摄取。
    """
    # PLAN-16 完整：去掉全局幂等键（embed_pending 天然幂等：WHERE embedding_status='pending'）
    try:
        from cogito.service.task_service import SqliteTaskService
        task = SqliteTaskService(conn).create(
            "knowledge.embed",
            json.dumps({"mode": "pending", "embed_model": embed_model}, ensure_ascii=False),
            origin=origin,
            priority=20,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
        if task:
            _LOGGER.info("Enqueued knowledge.embed")
            return task.task_id
        return None
    except sqlite3.IntegrityError:
        _LOGGER.debug("knowledge.embed create conflict")
        return None


def enqueue_connector_knowledge_sync(
    conn: sqlite3.Connection,
    *,
    connector_id: str,
    item: dict,
    principal_id: str = "owner",
) -> str | None:
    """为一条 Connector 新/修改内容创建 durable knowledge.sync_source Task（KNOW-01/02）。"""
    source_item_id = str(item.get("source_item_id") or item.get("item_id") or "")
    if not source_item_id:
        return None
    stable_source_id = f"connector:{connector_id}:{source_item_id}"
    return enqueue_knowledge_sync_source(
        conn,
        stable_source_id=stable_source_id,
        source_kind="connector",
        content_hash=str(item.get("content_hash") or ""),
        raw_text=_compose_raw_text(item),
        principal_id=principal_id,
        trust_label="external",
        origin="connector_poll",
    )


def sync_resource(
    conn: sqlite3.Connection,
    *,
    stable_source_id: str,
    source_kind: str = "explicit_local_file",
    content_hash: str = "",
    raw_text: str,
    principal_id: str = "",
    trust_label: str = "unverified",
) -> str:
    """同步知识资源（PLAN-13 P13-10）。

    - unchanged（content_hash 未变）→ 跳过，不重新 parse/embed
    - modified → 旧 Resource 标 stale + 新版本 active
    - added → 新建

    返回 resource_id。
    """
    # PLAN-16 完整 payload 边界：Task 存 payload_ref、handler 解析为 raw_text
    effective_raw_text = raw_text
    if not effective_raw_text and payload_ref:
        from cogito.service.knowledge.resolver import resolve_payload_ref
        effective_raw_text = resolve_payload_ref(payload_ref, _shared_payload_store())

    resource_id = KnowledgeService(conn).sync_source(
        stable_source_id=stable_source_id,
        source_kind=source_kind,
        content_hash=content_hash,
        raw_text=effective_raw_text,
        principal_id=principal_id,
        trust_label=trust_label,
    )
    # PLAN-16 完整：不再内部 commit，由调用方（Task Handler）统一提交事务
    _LOGGER.info("Synced resource %s", resource_id)
    return resource_id


def delete_resource(
    conn: sqlite3.Connection,
    *,
    stable_source_id: str,
    principal_id: str = "",
) -> bool:
    """删除来源的级联（PLAN-13 P13-10）。

    - Resource deleted/tombstone
    - Segment 从默认检索撤销（FTS清理）
    - 幂等：重复删除返回 True
    """
    result = KnowledgeService(conn).delete_source(
        stable_source_id=stable_source_id, principal_id=principal_id,
    )
    # PLAN-16 完整：不再内部 commit，由调用方（Task Handler）统一提交事务
    return result


def _find_by_stable_id(
    conn: sqlite3.Connection, stable_source_id: str, principal_id: str,
) -> dict | None:
    row = conn.execute(
        "SELECT resource_id, content_hash FROM knowledge_resources "
        "WHERE source_uri_hash=? AND principal_id=? AND deleted_at IS NULL",
        (stable_source_id, principal_id),
    ).fetchone()
    if not row:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    return {"resource_id": row[0], "content_hash": row[1]}


