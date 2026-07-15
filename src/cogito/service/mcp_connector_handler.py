"""MCP Connector 摄取 Task Handler —— 调用 MCP Tool，标准化，去重，决策。

有界步骤（参照 Plan 06 / 7.1）：
1. 读取 Connector + MCP 映射配置 + Cursor
2. 创建 IngestionBatch(started)
3. 事务外分页调用 MCP Tool（受 max_pages / max_items 预算）
4. 校验结构化结果 + schema_hash
5. 归档 Raw Payload
6. 逐项 Normalize / Quarantine
7. 短事务写 ConnectorItem + Outbox(SourceEventIngested)
8. 成功后 CAS 推进 Cursor
9. 完成 IngestionBatch
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from cogito.capability.mcp.client import (
    MCPCallResult,
    MCPResultError,
)
from cogito.contracts.clock import now_ms
from cogito.domain.connector import (
    ConnectorCursor,
    ConnectorItem,
    ConnectorRawItem,
    ConnectorStatus,
    ItemStatus,
)
from cogito.domain.events import DomainEvent
from cogito.domain.mcp_connector import MCPConnectorConfig
from cogito.domain.task import Task
from cogito.service.relevance import decide, score_relevance
from cogito.service.summary_service import summarize_item
from cogito.service.task_handlers import TaskHandlerContext
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.connector_repo import (
    ConnectorCursorRepository,
    ConnectorItemRepository,
    ConnectorRawRepository,
    ConnectorRepository,
)
from cogito.store.mcp_connector_repo import MCPConnectorConfigRepository
from cogito.store.repositories import OutboxRepository

_LOGGER = logging.getLogger(__name__)


def handle_mcp_connector_poll(task: Task, ctx: TaskHandlerContext) -> str:
    """mcp_connector.poll 任务处理器。"""
    connector_id = task.payload_ref
    if not connector_id:
        return "mcp poll skipped: empty connector_id"

    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "mcp poll skipped: no connection_factory"

    try:
        result = _poll_mcp_connector(conn, connector_id, ctx)
        try:
            conn.close()
        except Exception:
            pass
        return result
    except Exception:
        _LOGGER.exception("mcp_connector.poll failed: %s", connector_id)
        try:
            # 任何阶段失败都必须收尾 batch；不仅分页调用会失败，Normalize、
            # Outbox 或 Cursor 提交也可能抛错。按 task_id 限定，避免误伤并发批次。
            conn.execute(
                "UPDATE ingestion_batches SET status='failed', "
                "error_ref=CASE WHEN error_ref='' THEN 'task_handler_failed' ELSE error_ref END, "
                "completed_at=? WHERE connector_id=? AND task_id=? AND status='started'",
                (now_ms(), connector_id, task_id_from_ctx(ctx)),
            )
            ConnectorRepository(conn).update_failure(connector_id)
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            conn.close()
        except Exception:
            pass
        raise


# ── 内部工具：规范化 ─────────────────────────────────────────────────────────


def _normalize_dt(value: Any) -> datetime | None:
    """把多种时间格式规范化为 datetime。"""
    if value is None or value == "":
        return None
    # numeric epoch ms (int/float)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, tz=UTC)
        except (OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        # ISO 8601
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                continue
    return None


def _build_source_metadata(item: Any) -> str:
    """把 MCP 整条 item 存为 source_metadata_json（用于回放 / 审计）。"""
    return json.dumps(item if isinstance(item, dict) else {"_raw": str(item)}, ensure_ascii=False)[
        :65536
    ]


# ── 主流程 ───────────────────────────────────────────────────────────────────


def _poll_mcp_connector(
    conn: sqlite3.Connection,
    connector_id: str,
    ctx: TaskHandlerContext,
) -> str:
    conn.row_factory = sqlite3.Row

    connector = ConnectorRepository(conn).get(connector_id)
    if connector is None:
        return f"poll skipped: connector {connector_id} not found"
    if connector.status not in (ConnectorStatus.active, ConnectorStatus.error):
        return f"poll skipped: connector status={connector.status.value}"

    mapping = MCPConnectorConfigRepository(conn).get(connector_id)
    if mapping is None:
        return f"poll skipped: connector {connector_id} has no mcp mapping"

    # 1. 只支持已启动的 MCP Server
    mcp_manager = getattr(ctx, "mcp_manager", None)
    if mcp_manager is None:
        return "poll skipped: mcp_manager not configured"
    client = mcp_manager.get_client(mapping.server_name)
    if client is None or not client.connected:
        raise RuntimeError(
            f"mcp server '{mapping.server_name}' not connected — "
            "will retry on next scheduled attempt"
        )

    # 同步调用 helper（避免 MCP anyio task_group 与外部 loop 冲突）
    def _call_tool(args: dict[str, Any]) -> MCPCallResult:
        return mcp_manager.call_tool_structured_sync(
            mapping.server_name,
            mapping.tool_name,
            args,
            max_output_bytes=mapping.max_output_bytes,
        )

    cursor = ConnectorCursorRepository(conn).get(connector_id)
    now = datetime.now(UTC)

    # 2. 创建 IngestionBatch(started)
    batch_id = uuid.uuid4().hex
    before_cursor = cursor.cursor_json if cursor else {}
    IngestionBatchRepository(conn).insert_started(
        batch_id=batch_id,
        connector_id=connector_id,
        task_id=task_id_from_ctx(ctx),
        attempt_id=attempt_id_from_ctx(ctx),
        cursor_before=before_cursor,
        started_at=now,
    )

    # 3. 事务外分页拉取
    fetched_items: list[dict[str, Any]] = []
    last_schema_hash = ""
    pages = 0
    next_cursor: str | None = _resolve_cursor(cursor, mapping)

    try:
        while (
            pages < mapping.max_pages_per_poll and len(fetched_items) < mapping.max_items_per_poll
        ):
            args = dict(mapping.arguments_template)
            if next_cursor:
                args["cursor"] = next_cursor
            call_result: MCPCallResult = _call_tool(args)
            last_schema_hash = call_result.schema_hash

            if call_result.is_error:
                raise RuntimeError(f"mcp tool call failed: {call_result.text_content[:200]}")

            structured = call_result.structured_content
            if not isinstance(structured, dict):
                raise MCPResultError(
                    f"expected JSON object, got {type(structured).__name__}",
                )

            items = mapping.resolve_path(structured, mapping.items_path) or []
            if not isinstance(items, list):
                items = [items]

            fetched_items.extend(it for it in items if isinstance(it, dict))

            # 翻页：优先 next_cursor；其次 has_more=false 停止
            more = mapping.resolve_path(structured, mapping.has_more_path)
            next_cursor = _as_str(mapping.resolve_path(structured, mapping.next_cursor_path))
            pages += 1

            if not next_cursor or more is False:
                break

            if len(fetched_items) >= mapping.max_items_per_poll:
                break

    except Exception as e:
        _LOGGER.error("mcp poll %s fetch error: %s", connector_id, e)
        IngestionBatchRepository(conn).mark_failed(batch_id, str(e)[:1000])
        ConnectorRepository(conn).update_failure(connector_id)
        raise

    # 4. Normalize / score / summarize outside any write transaction.
    # MODEL-ADAPTER and RUNTIME-FLOWS explicitly forbid holding SQLite writes
    # while calling a model or an external service.
    raw = ConnectorRawItem(
        connector_id=connector_id,
        source_item_id=f"mcp-{now_ms()}",
        content_hash=last_schema_hash,
        payload_ref=None,
        http_etag="",
        http_last_modified="",
    )
    new_count = 0
    dup_count = 0
    quarantined_count = 0
    prepared_items: list[dict[str, Any]] = []

    for item in fetched_items:
        shutdown_requested = getattr(ctx, "shutdown_requested", None)
        if shutdown_requested is not None and shutdown_requested():
            raise RuntimeError("shutdown requested during MCP connector enrichment")

        external_id = mapping.extract_item_field(item, mapping.stable_id_path)
        if not external_id:
            quarantined_count += 1
            continue

        # Avoid model work for items already known before entering the write UoW.
        if ConnectorItemRepository(conn).find_by_source_id(connector_id, external_id):
            dup_count += 1
            continue

        title = mapping.extract_item_field(item, mapping.title_path)
        body = mapping.extract_item_field(item, mapping.body_path)
        url = mapping.extract_item_field(item, mapping.url_path)
        topic = mapping.extract_item_field(item, mapping.topic_path)
        occurred_at = _normalize_dt(mapping.resolve_path(item, mapping.updated_at_path))
        content_hash = _content_hash(external_id, title, body, url)
        if ConnectorItemRepository(conn).find_by_content_hash(connector_id, content_hash):
            dup_count += 1
            continue

        # Deterministic relevance is independent from optional LLM summarization.
        relevance = score_relevance(title, body, occurred_at, interests=[])
        decision = decide(relevance, threshold=0.4)
        item_status = ItemStatus.digest if decision == "digest" else ItemStatus.silent
        summary_text = ""
        model_router = getattr(ctx, "model_router", None)
        if model_router is not None and (title or body):
            try:
                summary_text = asyncio.run(summarize_item(title, body, model_router))
            except Exception:
                _LOGGER.warning("summary failed for %s", external_id)

        prepared_items.append(
            {
                "item": item,
                "external_id": external_id,
                "title": title,
                "body": body,
                "url": url,
                "topic": topic,
                "occurred_at": occurred_at,
                "content_hash": content_hash,
                "relevance": relevance,
                "summary_text": summary_text,
                "item_status": item_status,
            }
        )

    # 5. Persist Raw + normalized items + Outbox + Cursor in one short UoW.
    with UnitOfWork(conn) as uow:
        ConnectorRawRepository(conn).insert(raw)

        # Cursor 准备
        next_cursor_json = dict(cursor.cursor_json) if cursor else {}
        if next_cursor:
            next_cursor_json["cursor"] = next_cursor
        next_cursor_json["last_item_ids"] = [
            mapping.extract_item_field(it, mapping.stable_id_path) or uuid.uuid4().hex
            for it in fetched_items[:200]
        ]
        next_cursor_json["schema_hash"] = last_schema_hash
        next_cursor_json["items_count"] = len(fetched_items)
        next_cursor_json["pages"] = pages

        for prepared in prepared_items:
            item = prepared["item"]
            external_id = prepared["external_id"]
            content_hash = prepared["content_hash"]

            # Re-check under the write transaction to close the concurrent poll race.
            if ConnectorItemRepository(conn).find_by_source_id(connector_id, external_id):
                dup_count += 1
                continue
            if ConnectorItemRepository(conn).find_by_content_hash(connector_id, content_hash):
                dup_count += 1
                continue

            connector_item = ConnectorItem(
                connector_id=connector_id,
                raw_item_id=raw.raw_item_id,
                source_item_id=external_id,
                title=prepared["title"][:1000],
                link=prepared["url"][:2000],
                summary=prepared["body"][:8000],
                author="",
                published_at=prepared["occurred_at"],
                content_hash=content_hash,
                relevance=prepared["relevance"],
                summary_text=prepared["summary_text"][:4000],
                status=prepared["item_status"],
                topic=prepared["topic"][:200] if prepared["topic"] else "general",
            )
            ConnectorItemRepository(conn).insert(
                connector_item, source_metadata=_build_source_metadata(item)
            )

            # PLAN-16 M4 KNOW-02: MCP 新 digest 内容经 durable Task 进入 Knowledge
            if prepared["item_status"] == ItemStatus.digest:
                from cogito.service.knowledge.sync import enqueue_connector_knowledge_sync

                enqueue_connector_knowledge_sync(
                    conn,
                    connector_id=connector_id,
                    item={
                        "source_item_id": external_id,
                        "title": prepared["title"] or "",
                        "body": prepared["body"] or "",
                        "content_hash": content_hash,
                    },
                    principal_id="owner",
                    # PLAN-16 P16-13：大正文写 PayloadStore，不内联
                    make_payload_store=ctx.payload_store_factory,
                )

            # 6. 发 Outbox —— SourceEventIngested
            event = DomainEvent(
                event_type="SourceEventIngested",
                aggregate_type="source",
                aggregate_id=external_id,
                aggregate_version=1,
                payload_ref=connector_item.item_id,
                payload=None,
                content_hash=content_hash,
                trust_label="external_untrusted",
                schema_version=mapping.config_version,
                correlation_id=batch_id,
                origin=f"mcp:{mapping.server_name}:{mapping.tool_name}",
            )
            OutboxRepository(conn).insert(event)
            new_count += 1

        # 7. 成功后推进 cursor
        ConnectorCursorRepository(conn).upsert(
            ConnectorCursor(
                connector_id=connector_id,
                etag="",
                last_modified="",
                last_item_ids=next_cursor_json.get("last_item_ids", []),
                last_polled_at=now,
                cursor_json=next_cursor_json,
            )
        )
        ConnectorRepository(conn).update_success(connector_id)

        uow.commit()

    IngestionBatchRepository(conn).mark_committed(
        batch_id=batch_id,
        cursor_after=next_cursor_json,
        fetched=len(fetched_items),
        accepted=new_count,
        duplicate=dup_count,
        quarantined=quarantined_count,
        completed_at=datetime.now(UTC),
    )

    _LOGGER.info(
        "mcp_connector.poll: %s pages=%d fetched=%d new=%d dup=%d q=%d",
        connector_id,
        pages,
        len(fetched_items),
        new_count,
        dup_count,
        quarantined_count,
    )
    return (
        f"pages={pages} fetched={len(fetched_items)} "
        f"new={new_count} dup={dup_count} q={quarantined_count}"
    )


# ── 辅助 ─────────────────────────────────────────────────────────────────────


def _content_hash(external_id: str, title: str, body: str, url: str) -> str:
    import hashlib

    raw = "\n".join([external_id, title, url, body[:500]])
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _resolve_cursor(cursor: ConnectorCursor | None, mapping: MCPConnectorConfig) -> str | None:
    if cursor is None or not cursor.cursor_json:
        return None
    return cursor.cursor_json.get("cursor") or None


def _as_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def task_id_from_ctx(ctx: TaskHandlerContext) -> str:
    return getattr(ctx, "_task_id", "") or ""


def attempt_id_from_ctx(ctx: TaskHandlerContext) -> str:
    return getattr(ctx, "_attempt_id", "") or ""


class IngestionBatchRepository:
    """ingestion_batches 的轻量读写。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_started(
        self,
        batch_id: str,
        connector_id: str,
        task_id: str,
        attempt_id: str,
        cursor_before: dict,
        started_at: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO ingestion_batches
                (batch_id, connector_id, task_id, attempt_id, status,
                 cursor_before_json, started_at)
            VALUES (?, ?, ?, ?, 'started', ?, ?)
            """,
            (
                batch_id,
                connector_id,
                task_id,
                attempt_id,
                json.dumps(cursor_before, ensure_ascii=False),
                now_ms(),
            ),
        )
        self._conn.commit()

    def mark_committed(
        self,
        batch_id: str,
        cursor_after: dict,
        fetched: int,
        accepted: int,
        duplicate: int,
        quarantined: int,
        completed_at: datetime,
    ) -> None:
        self._conn.execute(
            """
            UPDATE ingestion_batches
            SET status='committed', cursor_after_json=?,
                fetched_count=?, accepted_count=?,
                duplicate_count=?, quarantined_count=?,
                completed_at=?
            WHERE batch_id=?
            """,
            (
                json.dumps(cursor_after, ensure_ascii=False),
                fetched,
                accepted,
                duplicate,
                quarantined,
                now_ms(),
                batch_id,
            ),
        )
        self._conn.commit()

    def mark_failed(self, batch_id: str, error_ref: str) -> None:
        self._conn.execute(
            """
            UPDATE ingestion_batches
            SET status='failed', error_ref=?, completed_at=?
            WHERE batch_id=?
            """,
            (error_ref[:1000], now_ms(), batch_id),
        )
        self._conn.commit()
