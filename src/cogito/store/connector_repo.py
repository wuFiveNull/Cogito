"""Connector / Cursor / RawItem / Item 数据访问层。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from cogito.domain.connector import (
    Connector,
    ConnectorCursor,
    ConnectorItem,
    ConnectorRawItem,
    ConnectorStatus,
    ItemStatus,
)
from cogito.store.time_utils import epoch_ms, from_epoch_ms


class ConnectorRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, connector_id: str) -> Connector | None:
        row = self._conn.execute(
            "SELECT * FROM connectors WHERE connector_id=?", (connector_id,),
        ).fetchone()
        return self._row_to_connector(row) if row else None

    def insert(self, connector: Connector) -> None:
        self._conn.execute(
            "INSERT INTO connectors (connector_id, connector_type, name, url, "
            "  site_link, poll_schedule_id, fetch_timeout_s, status, "
            "  consecutive_failures, created_at) "
            "VALUES (?,?,?,?,?, ?,?,?,?)",
            (
                connector.connector_id,
                connector.connector_type.value,
                connector.name,
                connector.url,
                connector.site_link,
                connector.poll_schedule_id,
                connector.fetch_timeout_s,
                connector.status.value,
                connector.consecutive_failures,
                epoch_ms(connector.created_at),
            ),
        )

    def find_active(self, limit: int = 20) -> list[Connector]:
        rows = self._conn.execute(
            "SELECT * FROM connectors WHERE status='active' LIMIT ?", (limit,),
        ).fetchall()
        return [self._row_to_connector(r) for r in rows]

    def update_status(self, connector_id: str, status: ConnectorStatus) -> None:
        self._conn.execute(
            "UPDATE connectors SET status=? WHERE connector_id=?",
            (status.value, connector_id),
        )

    def update_success(self, connector_id: str) -> None:
        now_ms = epoch_ms(datetime.now(UTC))
        self._conn.execute(
            "UPDATE connectors SET last_success_at=?, last_attempt_at=?, "
            "  consecutive_failures=0, status='active' WHERE connector_id=?",
            (now_ms, now_ms, connector_id),
        )

    def update_failure(self, connector_id: str) -> None:
        now_ms = epoch_ms(datetime.now(UTC))
        self._conn.execute(
            "UPDATE connectors SET last_attempt_at=?, "
            "  consecutive_failures=consecutive_failures+1 WHERE connector_id=?",
            (now_ms, connector_id),
        )

    @staticmethod
    def _row_to_connector(row: sqlite3.Row) -> Connector:
        return Connector(
            connector_id=row["connector_id"],
            connector_type=row["connector_type"],
            name=row["name"],
            url=row["url"],
            site_link=row["site_link"],
            poll_schedule_id=row["poll_schedule_id"],
            fetch_timeout_s=row["fetch_timeout_s"],
            status=row["status"],
            consecutive_failures=row["consecutive_failures"],
            created_at=from_epoch_ms(row["created_at"]),
        )


class ConnectorCursorRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, connector_id: str) -> ConnectorCursor | None:
        row = self._conn.execute(
            "SELECT * FROM connector_cursors WHERE connector_id=?", (connector_id,),
        ).fetchone()
        if row is None:
            return None
        last_ids = json.loads(row["last_item_ids"] or "[]")
        return ConnectorCursor(
            connector_id=row["connector_id"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_item_ids=last_ids,
            last_polled_at=from_epoch_ms(row["last_polled_at"]),
            cursor_json=json.loads(row["cursor_json"] or "{}"),
            updated_at=from_epoch_ms(row["updated_at"]),
        )

    def upsert(self, cursor: ConnectorCursor) -> None:
        now_ms = epoch_ms(datetime.now(UTC))
        self._conn.execute(
            "INSERT INTO connector_cursors "
            "(connector_id, etag, last_modified, last_item_ids, "
            " last_polled_at, cursor_json, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(connector_id) DO UPDATE SET "
            "etag=excluded.etag, last_modified=excluded.last_modified, "
            "last_item_ids=excluded.last_item_ids, last_polled_at=excluded.last_polled_at, "
            "cursor_json=excluded.cursor_json, updated_at=excluded.updated_at",
            (
                cursor.connector_id,
                cursor.etag,
                cursor.last_modified,
                json.dumps(cursor.last_item_ids),
                epoch_ms(cursor.last_polled_at),
                json.dumps(cursor.cursor_json),
                now_ms,
            ),
        )


class ConnectorRawRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, raw: ConnectorRawItem) -> None:
        self._conn.execute(
            "INSERT INTO connector_raw_items "
            "(raw_item_id, connector_id, source_item_id, fetched_at, "
            " content_hash, payload_ref, http_etag, http_last_modified) "
            "VALUES (?,?,?,?,?, ?,?,?)",
            (
                raw.raw_item_id,
                raw.connector_id,
                raw.source_item_id,
                epoch_ms(raw.fetched_at),
                raw.content_hash,
                raw.payload_ref,
                raw.http_etag,
                raw.http_last_modified,
            ),
        )

    def find_by_content_hash(self, connector_id: str, content_hash: str) -> ConnectorRawItem | None:
        row = self._conn.execute(
            "SELECT * FROM connector_raw_items "
            "WHERE connector_id=? AND content_hash=?",
            (connector_id, content_hash),
        ).fetchone()
        return self._row_to_raw(row) if row else None

    @staticmethod
    def _row_to_raw(row: sqlite3.Row) -> ConnectorRawItem:
        return ConnectorRawItem(
            raw_item_id=row["raw_item_id"],
            connector_id=row["connector_id"],
            source_item_id=row["source_item_id"],
            fetched_at=from_epoch_ms(row["fetched_at"]),
            content_hash=row["content_hash"],
            payload_ref=row["payload_ref"],
            http_etag=row["http_etag"],
            http_last_modified=row["http_last_modified"],
        )


class ConnectorItemRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, item: ConnectorItem) -> None:
        self._conn.execute(
            "INSERT INTO connector_items "
            "(item_id, connector_id, raw_item_id, source_item_id, title, link, "
            " summary, author, published_at, content_hash, relevance, "
            " summary_text, status, created_at) "
            "VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?)",
            (
                item.item_id,
                item.connector_id,
                item.raw_item_id,
                item.source_item_id,
                item.title,
                item.link,
                item.summary,
                item.author,
                epoch_ms(item.published_at),
                item.content_hash,
                item.relevance,
                item.summary_text,
                item.status.value,
                epoch_ms(item.created_at),
            ),
        )

    def find_by_source_id(self, connector_id: str, source_item_id: str) -> ConnectorItem | None:
        row = self._conn.execute(
            "SELECT * FROM connector_items "
            "WHERE connector_id=? AND source_item_id=?",
            (connector_id, source_item_id),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def find_by_content_hash(self, connector_id: str, content_hash: str) -> ConnectorItem | None:
        row = self._conn.execute(
            "SELECT * FROM connector_items "
            "WHERE connector_id=? AND content_hash=?",
            (connector_id, content_hash),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def update_status(self, item_id: str, status: ItemStatus) -> None:
        self._conn.execute(
            "UPDATE connector_items SET status=? WHERE item_id=?",
            (status.value, item_id),
        )

    def update_summary(self, item_id: str, summary_text: str, relevance: float) -> None:
        self._conn.execute(
            "UPDATE connector_items SET summary_text=?, relevance=? WHERE item_id=?",
            (summary_text, relevance, item_id),
        )

    def find_by_status(
        self, connector_id: str, status: ItemStatus, limit: int = 50,
    ) -> list[ConnectorItem]:
        rows = self._conn.execute(
            "SELECT * FROM connector_items "
            "WHERE connector_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
            (connector_id, status.value, limit),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def find_all(self, limit: int = 100) -> list[ConnectorItem]:
        rows = self._conn.execute(
            "SELECT * FROM connector_items ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ConnectorItem:
        return ConnectorItem(
            item_id=row["item_id"],
            connector_id=row["connector_id"],
            raw_item_id=row["raw_item_id"],
            source_item_id=row["source_item_id"],
            title=row["title"],
            link=row["link"],
            summary=row["summary"],
            author=row["author"],
            published_at=from_epoch_ms(row["published_at"]),
            content_hash=row["content_hash"],
            relevance=row["relevance"],
            summary_text=row["summary_text"],
            status=row["status"],
            created_at=from_epoch_ms(row["created_at"]),
        )
