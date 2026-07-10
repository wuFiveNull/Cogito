"""connector.poll Task Handler —— 抓取 + 归档 + 去重 + 摘要 + 相关性 + 决策。

完整流程：
1. 从 task.payload_ref 读 connector_id
2. RssFetcher.fetch() → 304 跳过 / 失败抛异常（走 Task 重试）
3. 归档 raw item
4. 逐条去重（source_id → content_hash）
5. 新条目：摘要（模型）+ 相关性（启发式）+ 决策（digest/silent）
6. 推进 cursor
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from cogito.domain.connector import (
    ConnectorCursor,
    ConnectorItem,
    ConnectorRawItem,
    ItemStatus,
)
from cogito.domain.task import Task
from cogito.service.relevance import decide, score_relevance
from cogito.service.rss_fetcher import (
    Fetched,
    FetchFailed,
    NotModified,
    RssFetcher,
)
from cogito.service.summary_service import summarize_item
from cogito.service.task_handlers import TaskHandlerContext
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.connector_repo import (
    ConnectorCursorRepository,
    ConnectorItemRepository,
    ConnectorRawRepository,
    ConnectorRepository,
)
from cogito.contracts.clock import epoch_ms

_LOGGER = logging.getLogger(__name__)


def handle_connector_poll(task: Task, ctx: TaskHandlerContext) -> str:
    """connector.poll 任务处理器。"""
    connector_id = task.payload_ref
    if not connector_id:
        return "poll skipped: empty connector_id"

    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "poll skipped: no connection_factory"

    try:
        result = _poll_connector(conn, connector_id, ctx)
        try:
            conn.close()
        except Exception:
            pass
        return result
    except Exception:
        _LOGGER.exception("connector.poll failed: %s", connector_id)
        try:
            conn.close()
        except Exception:
            pass
        raise


def _poll_connector(
    conn: sqlite3.Connection, connector_id: str, ctx: TaskHandlerContext,
) -> str:
    conn.row_factory = sqlite3.Row

    connector = ConnectorRepository(conn).get(connector_id)
    if connector is None:
        return f"poll skipped: connector {connector_id} not found"
    if connector.status.value not in ("active", "error"):
        return f"poll skipped: connector status={connector.status.value}"

    cursor = ConnectorCursorRepository(conn).get(connector_id)

    # 1. 抓取
    fetcher = RssFetcher()
    import asyncio

    fetch_result = asyncio.run(fetcher.fetch(connector, cursor))

    if isinstance(fetch_result, NotModified):
        _LOGGER.info("connector.poll: %s not modified", connector_id)
        ConnectorCursorRepository(conn).upsert(ConnectorCursor(
            connector_id=connector_id,
            etag=cursor.etag if cursor else "",
            last_modified=cursor.last_modified if cursor else "",
            last_polled_at=datetime.now(UTC),
        ))
        return "not modified"

    if isinstance(fetch_result, FetchFailed):
        ConnectorRepository(conn).update_failure(connector_id)
        if fetch_result.retryable:
            raise RuntimeError(
                f"fetch failed (retryable): {fetch_result.message}",
            )
        return f"fetch failed (non-retryable): {fetch_result.message}"

    assert isinstance(fetch_result, Fetched)

    # 2. 推进 cursor + 更新 connector 状态
    new_item_ids = [e.source_item_id for e in fetch_result.entries]
    now = datetime.now(UTC)
    ConnectorCursorRepository(conn).upsert(ConnectorCursor(
        connector_id=connector_id,
        etag=fetch_result.new_etag,
        last_modified=fetch_result.new_last_modified,
        last_item_ids=new_item_ids,
        last_polled_at=now,
    ))
    ConnectorRepository(conn).update_success(connector_id)

    # 3. 归档 raw
    raw = ConnectorRawItem(
        connector_id=connector_id,
        source_item_id=f"feed-{epoch_ms(now)}",
        content_hash=fetch_result.raw_content_hash,
        payload_ref=None,  # 可扩展存 payload_objects
        http_etag=fetch_result.new_etag,
        http_last_modified=fetch_result.new_last_modified,
    )
    ConnectorRawRepository(conn).insert(raw)

    # 4. 逐条去重 + 处理
    new_count = 0
    dup_count = 0
    model_router = ctx.model_router

    with UnitOfWork(conn) as uow:
        for entry in fetch_result.entries:
            existing = ConnectorItemRepository(conn).find_by_source_id(
                connector_id, entry.source_item_id,
            )
            if existing is None:
                existing = ConnectorItemRepository(conn).find_by_content_hash(
                    connector_id, entry.content_hash,
                )

            if existing is not None:
                dup_count += 1
                continue

            # 新条目：摘要 + 相关性 + 决策
            summary_text = ""
            relevance = 0.0
            item_status = ItemStatus.new

            if model_router is not None:
                try:
                    summary_text = asyncio.run(summarize_item(
                        entry.title, entry.summary, model_router,
                    ))
                except Exception:
                    _LOGGER.warning("summary failed for %s", entry.source_item_id)

                try:
                    relevance = score_relevance(
                        entry.title, entry.summary, entry.published_at,
                        interests=[],  # 可由 ctx 传递配置
                    )
                    decision = decide(relevance, threshold=0.4)
                    item_status = ItemStatus.digest if decision == "digest" else ItemStatus.silent
                except Exception:
                    _LOGGER.warning("relevance failed for %s", entry.source_item_id)

            item = ConnectorItem(
                connector_id=connector_id,
                raw_item_id=raw.raw_item_id,
                source_item_id=entry.source_item_id,
                title=entry.title,
                link=entry.link,
                summary=entry.summary,
                author=entry.author,
                published_at=entry.published_at,
                content_hash=entry.content_hash,
                relevance=relevance,
                summary_text=summary_text,
                status=item_status,
            )
            ConnectorItemRepository(conn).insert(item)
            new_count += 1

        uow.commit()

    _LOGGER.info(
        "connector.poll: %s fetched=%d new=%d dup=%d",
        connector_id, len(fetch_result.entries), new_count, dup_count,
    )
    return f"fetched={len(fetch_result.entries)} new={new_count} dup={dup_count}"
