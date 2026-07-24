"""Connector / Cursor / RawItem / Item 数据访问层 — Event-only.

Write operations append canonical Events.  Read operations replay from Event
streams.  Legacy table writes are kept as rebuildable projections for FK
compatibility during cutover.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from cogito.contracts.clock import epoch_ms, from_epoch_ms
from cogito.domain.connector import (
    Connector,
    ConnectorCursor,
    ConnectorItem,
    ConnectorRawItem,
    ConnectorStatus,
    ConnectorType,
    ItemStatus,
)
from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_replay import (
    ConnectorProjection,
    ConnectorSourceProjection,
    replay_connector,
    replay_connector_source,
)
from cogito.store.event_store import EventStore


class ConnectorRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, connector_id: str) -> Connector | None:
        state = replay_connector(
            EventStore(self._conn).read_stream("connector", connector_id), connector_id
        )
        return self._projection_to_connector(state) if state is not None else None

    def insert(self, connector: Connector) -> None:
        EventStore(self._conn).append(
            Event(
                event_type="connector.created",
                stream_type="connector",
                stream_id=connector.connector_id,
                producer="connector-repository",
                event_class=EventClass.DOMAIN,
                summary=f"Connector created: {connector.name}",
                attributes={
                    "connector_type": connector.connector_type.value,
                    "name": connector.name,
                    "url": connector.url or "",
                    "site_link": connector.site_link or "",
                    "poll_schedule_id": connector.poll_schedule_id or "",
                    "fetch_timeout_s": connector.fetch_timeout_s,
                },
                outcome=connector.status.value,
                occurred_at=epoch_ms(connector.created_at),
                idempotency_key=f"connector:{connector.connector_id}:created",
            ),
            expected_version=0,
        )

    def find_active(self, limit: int = 20) -> list[Connector]:
        connectors = self._event_connectors()
        active = [c for c in connectors if c.status.value == "active"]
        return active[:limit]

    def update_status(self, connector_id: str, status: ConnectorStatus) -> None:
        EventStore(self._conn).append(
            Event(
                event_type="connector.status.updated",
                stream_type="connector",
                stream_id=connector_id,
                producer="connector-repository",
                event_class=EventClass.DOMAIN,
                summary=f"Connector status: {status.value}",
                attributes={"status": status.value},
                outcome=status.value,
                idempotency_key=f"connector:{connector_id}:status:{status.value}",
            ),
        )

    def update_success(self, connector_id: str) -> None:
        now_ms = epoch_ms(datetime.now(UTC))
        EventStore(self._conn).append(
            Event(
                event_type="connector.status.updated",
                stream_type="connector",
                stream_id=connector_id,
                producer="connector-repository",
                event_class=EventClass.DOMAIN,
                summary="Connector poll succeeded",
                attributes={"status": "active"},
                outcome="active",
                idempotency_key=f"connector:{connector_id}:success:{now_ms}",
            ),
        )

    def update_failure(self, connector_id: str) -> None:
        now_ms = epoch_ms(datetime.now(UTC))
        # Append failure Event — successor to the old consecutive_failures counter
        EventStore(self._conn).append(
            Event(
                event_type="connector.status.updated",
                stream_type="connector",
                stream_id=connector_id,
                producer="connector-repository",
                event_class=EventClass.DOMAIN,
                summary="Connector poll failed",
                attributes={"status": "error", "last_attempt_at": now_ms},
                outcome="error",
                idempotency_key=f"connector:{connector_id}:failure:{now_ms}",
            ),
        )

    @staticmethod
    def _projection_to_connector(state: ConnectorProjection) -> Connector:
        return Connector(
            connector_id=state.connector_id,
            connector_type=ConnectorType(state.connector_type) if state.connector_type else ConnectorType.rss,
            name=state.name,
            url=state.url,
            status=ConnectorStatus(state.status) if state.status else ConnectorStatus.active,
        )

    def _event_connectors(self) -> list[Connector]:
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("connector"):
            grouped.setdefault(event.stream_id, []).append(event)
        return [
            self._projection_to_connector(state)
            for cid, stream in grouped.items()
            if (state := replay_connector(stream, cid)) is not None
        ]


class ConnectorCursorRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, connector_id: str) -> ConnectorCursor | None:
        """Reconstruct cursor from connector stream (connector.cursor.updated Events)."""
        events = EventStore(self._conn).read_stream("connector", connector_id)
        cursor_events = [e for e in events if e.event_type == "connector.cursor.updated"]
        if not cursor_events:
            return None
        latest = cursor_events[-1]
        attrs = latest.attributes
        last_item_ids = attrs.get("last_item_ids", [])
        if isinstance(last_item_ids, str):
            try:
                last_item_ids = json.loads(last_item_ids)
            except (TypeError, json.JSONDecodeError):
                last_item_ids = []
        return ConnectorCursor(
            connector_id=connector_id,
            etag=str(attrs.get("etag", "")),
            last_modified=str(attrs.get("last_modified", "")),
            last_item_ids=list(last_item_ids),
            last_polled_at=from_epoch_ms(latest.occurred_at) if latest.occurred_at else None,
            updated_at=from_epoch_ms(latest.occurred_at) if latest.occurred_at else None,
        )

    def upsert(self, cursor: ConnectorCursor) -> None:
        EventStore(self._conn).append(
            Event(
                event_type="connector.cursor.updated",
                stream_type="connector",
                stream_id=cursor.connector_id,
                producer="connector-repository",
                event_class=EventClass.OPERATION,
                summary="Connector cursor updated",
                attributes={
                    "etag": cursor.etag or "",
                    "last_modified": cursor.last_modified or "",
                    "last_item_ids": cursor.last_item_ids,
                },
                idempotency_key=f"connector:cursor:{cursor.connector_id}:{epoch_ms(datetime.now(UTC))}",
            ),
        )


class ConnectorRawRepository:
    """ConnectorRawItem 操作投影 — 以 Event 为真相源的可重建缓存。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, raw: ConnectorRawItem) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO connector_raw_items "
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
            "SELECT * FROM connector_raw_items WHERE connector_id=? AND content_hash=?",
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

    def insert(self, item: ConnectorItem, source_metadata: str = "") -> None:
        EventStore(self._conn).append(
            Event(
                event_type="connector.source.ingested",
                stream_type="source",
                stream_id=f"{item.connector_id}:{item.source_item_id}",
                producer="connector-repository",
                event_class=EventClass.DOMAIN,
                summary=f"Connector item ingested: {item.title or item.source_item_id}",
                attributes={
                    "item_id": item.item_id,
                    "connector_id": item.connector_id,
                    "source_item_id": item.source_item_id,
                    "title": item.title or "",
                    "link": item.link or "",
                    "content_hash": item.content_hash or "",
                    "published_at": epoch_ms(item.published_at) if item.published_at else None,
                    "topic": item.topic or "",
                },
                outcome=item.status.value,
                idempotency_key=f"connector:item:{item.connector_id}:{item.source_item_id}:ingested",
            ),
            expected_version=0,
        )

    def _replay_source(self, connector_id: str, source_item_id: str) -> ConnectorSourceProjection | None:
        """Replay a single source item from its Event stream."""
        stream_id = f"{connector_id}:{source_item_id}"
        events = EventStore(self._conn).read_stream("source", stream_id)
        return replay_connector_source(events, stream_id)

    def _replay_sources(self) -> dict[str, list[ConnectorSourceProjection]]:
        """Replay all source streams and group by connector_id."""
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("source"):
            grouped.setdefault(event.stream_id, []).append(event)
        result: dict[str, list[ConnectorSourceProjection]] = {}
        for sid, stream in grouped.items():
            proj = replay_connector_source(stream, sid)
            if proj is not None:
                # stream_id format: connector_id:source_item_id
                parts = sid.split(":", 1)
                cid = parts[0] if len(parts) > 1 else sid
                result.setdefault(cid, []).append(proj)
        return result

    def find_by_source_id(self, connector_id: str, source_item_id: str) -> ConnectorItem | None:
        proj = self._replay_source(connector_id, source_item_id)
        if proj is None:
            return None
        return ConnectorItem(
            item_id=proj.source_id or "",
            connector_id=connector_id,
            source_item_id=source_item_id,
            title=proj.title or "",
            link=proj.link or "",
            content_hash=proj.content_hash or "",
            status=ItemStatus(proj.status) if proj.status else ItemStatus.new,
        )

    def find_by_content_hash(self, connector_id: str, content_hash: str) -> ConnectorItem | None:
        for src_list in self._replay_sources().values():
            for proj in src_list:
                if proj.connector_id == connector_id and proj.content_hash == content_hash:
                    return ConnectorItem(
                        item_id=proj.source_id or "",
                        connector_id=connector_id,
                        source_item_id=proj.source_id or "",
                        title=proj.title or "",
                        link=proj.link or "",
                        content_hash=proj.content_hash or "",
                        status=ItemStatus(proj.status) if proj.status else ItemStatus.new,
                    )
        return None

    def update_status(self, item_id: str, status: ItemStatus) -> None:
        EventStore(self._conn).append(
            Event(
                event_type="connector.source.ingested",
                stream_type="source",
                stream_id=f"item:{item_id}",
                producer="connector-repository",
                event_class=EventClass.OPERATION,
                summary=f"Connector item status: {status.value}",
                attributes={"item_id": item_id, "status": status.value},
                idempotency_key=f"connector:item:{item_id}:status:{status.value}",
            ),
        )

    def update_summary(self, item_id: str, summary_text: str, relevance: float) -> None:
        EventStore(self._conn).append(
            Event(
                event_type="connector.source.ingested",
                stream_type="source",
                stream_id=f"item:{item_id}:summary",
                producer="connector-repository",
                event_class=EventClass.OPERATION,
                summary="Connector item summary updated",
                attributes={
                    "item_id": item_id,
                    "summary_text": summary_text,
                    "relevance": relevance,
                },
                idempotency_key=f"connector:item:{item_id}:summary:{hash(summary_text)}",
            ),
        )

    def find_by_status(
        self,
        connector_id: str,
        status: ItemStatus,
        limit: int = 50,
    ) -> list[ConnectorItem]:
        # Projection-only: maintained by consumers
        rows = self._conn.execute(
            "SELECT * FROM connector_items "
            "WHERE connector_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
            (connector_id, status.value, limit),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def find_all(self, limit: int = 100) -> list[ConnectorItem]:
        rows = self._conn.execute(
            "SELECT * FROM connector_items ORDER BY created_at DESC LIMIT ?",
            (limit,),
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
