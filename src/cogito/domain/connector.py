"""Connector 领域实体 —— RSS/Atom 数据源摄取。

CONNECTOR-INGESTION / 2. 对象: ConnectorInstance, ConnectorCursor,
RawItem, NormalizedItem, SourceEvent。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ConnectorType(StrEnum):
    rss = "rss"
    atom = "atom"
    json = "json"
    mcp = "mcp"


class ConnectorStatus(StrEnum):
    active = "active"
    paused = "paused"
    disabled = "disabled"
    error = "error"


class ItemStatus(StrEnum):
    new = "new"
    silent = "silent"
    digest = "digest"
    sent = "sent"
    duplicate = "duplicate"
    ignored = "ignored"


class Connector:
    """外部数据源配置。"""

    def __init__(
        self,
        connector_id: str | None = None,
        connector_type: ConnectorType = ConnectorType.rss,
        name: str = "",
        url: str = "",
        site_link: str = "",
        poll_schedule_id: str | None = None,
        fetch_timeout_s: int = 30,
        status: ConnectorStatus = ConnectorStatus.active,
        consecutive_failures: int = 0,
        last_success_at: datetime | None = None,
        last_attempt_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.connector_id = connector_id or uuid.uuid4().hex
        self.connector_type = ConnectorType(connector_type)
        self.name = name
        self.url = url
        self.site_link = site_link
        self.poll_schedule_id = poll_schedule_id
        self.fetch_timeout_s = fetch_timeout_s
        self.status = ConnectorStatus(status)
        self.consecutive_failures = consecutive_failures
        self.last_success_at = last_success_at
        self.last_attempt_at = last_attempt_at
        self.created_at = created_at or datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "connector_type": self.connector_type.value,
            "name": self.name,
            "url": self.url,
            "site_link": self.site_link,
            "poll_schedule_id": self.poll_schedule_id,
            "fetch_timeout_s": self.fetch_timeout_s,
            "status": self.status.value,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "created_at": self.created_at.isoformat(),
        }

    def __repr__(self) -> str:
        return f"Connector({self.connector_id}, {self.connector_type.value}, {self.name})"


class ConnectorCursor:
    """Connector 的拉取游标 —— ETag / Last-Modified / 已见条目 ID。"""

    def __init__(
        self,
        connector_id: str,
        etag: str = "",
        last_modified: str = "",
        last_item_ids: list[str] | None = None,
        last_polled_at: datetime | None = None,
        cursor_json: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        self.connector_id = connector_id
        self.etag = etag
        self.last_modified = last_modified
        self.last_item_ids = last_item_ids or []
        self.last_polled_at = last_polled_at
        self.cursor_json = cursor_json or {}
        self.updated_at = updated_at or datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "last_item_ids": self.last_item_ids,
            "last_polled_at": self.last_polled_at.isoformat() if self.last_polled_at else None,
            "cursor_json": self.cursor_json,
            "updated_at": self.updated_at.isoformat(),
        }


class ConnectorRawItem:
    """归档的原始内容（用于回放）。"""

    def __init__(
        self,
        raw_item_id: str | None = None,
        connector_id: str = "",
        source_item_id: str = "",
        fetched_at: datetime | None = None,
        content_hash: str = "",
        payload_ref: str | None = None,
        http_etag: str = "",
        http_last_modified: str = "",
    ) -> None:
        self.raw_item_id = raw_item_id or uuid.uuid4().hex
        self.connector_id = connector_id
        self.source_item_id = source_item_id
        self.fetched_at = fetched_at or datetime.now(UTC)
        self.content_hash = content_hash
        self.payload_ref = payload_ref
        self.http_etag = http_etag
        self.http_last_modified = http_last_modified

    def __repr__(self) -> str:
        return f"ConnectorRawItem({self.raw_item_id}, src={self.source_item_id})"


class ConnectorItem:
    """去重后的标准化条目。"""

    def __init__(
        self,
        item_id: str | None = None,
        connector_id: str = "",
        raw_item_id: str | None = None,
        source_item_id: str = "",
        title: str = "",
        link: str = "",
        summary: str = "",
        author: str = "",
        published_at: datetime | None = None,
        content_hash: str = "",
        relevance: float | None = None,
        summary_text: str = "",
        status: ItemStatus = ItemStatus.new,
        topic: str = "general",
        created_at: datetime | None = None,
    ) -> None:
        self.item_id = item_id or uuid.uuid4().hex
        self.connector_id = connector_id
        self.raw_item_id = raw_item_id
        self.source_item_id = source_item_id
        self.title = title
        self.link = link
        self.summary = summary
        self.author = author
        self.published_at = published_at
        self.content_hash = content_hash
        self.relevance = relevance
        self.summary_text = summary_text
        self.status = ItemStatus(status)
        self.topic = topic[:200] if topic else "general"
        self.created_at = created_at or datetime.now(UTC)

    def __repr__(self) -> str:
        return f"ConnectorItem({self.item_id}, {self.title[:40]!r}, {self.status.value})"


def compute_content_hash(title: str, link: str, summary: str) -> str:
    """计算条目内容 hash（规范化后）。"""
    normalized = "\n".join(
        (title.strip(), link.strip(), _strip_html(summary).strip()),
    )
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()


def _strip_html(text: str) -> str:
    """简单 HTML 标签剥离（不依赖 bs4）。"""
    import re
    return re.sub(r"<[^>]+>", "", text or "")
