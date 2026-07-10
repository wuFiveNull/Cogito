"""RssFetcher —— HTTP 抓取 + feedparser 解析 + ETag/Last-Modified 条件请求。

CONNECTOR-INGESTION / 3. Poll 协议、/ 4. Webhook、/ 5. 原始归档。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cogito.domain.connector import Connector, ConnectorCursor, compute_content_hash
from cogito.model.contracts import ErrorCategory

_LOGGER = logging.getLogger(__name__)


@dataclass
class RssEntry:
    """单条解析结果。"""
    source_item_id: str
    title: str
    link: str
    summary: str
    author: str
    published_at: datetime | None
    content_hash: str


@dataclass
class Fetched:
    """抓取成功结果。"""
    entries: list[RssEntry]
    new_etag: str
    new_last_modified: str
    raw_body: bytes
    raw_content_hash: str


@dataclass
class NotModified:
    """304 Not Modified。"""


@dataclass
class FetchFailed:
    """抓取失败。"""
    error_category: ErrorCategory
    retryable: bool
    message: str


RssFetchResult = Fetched | NotModified | FetchFailed


class RssFetcher:
    """RSS/Atom feed 抓取器。"""

    def __init__(self, http_client: Any = None) -> None:
        self._http = http_client

    async def fetch(
        self,
        connector: Connector,
        cursor: ConnectorCursor | None,
    ) -> RssFetchResult:
        """抓取 feed，支持条件请求（ETag / Last-Modified）。"""
        import httpx

        headers = {
            "User-Agent": "Cogito-Agent/0.1 (+https://github.com/hunriiz/cogito)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        }
        if cursor:
            if cursor.etag:
                headers["If-None-Match"] = cursor.etag
            if cursor.last_modified:
                headers["If-Modified-Since"] = cursor.last_modified

        client = self._http
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=connector.fetch_timeout_s, follow_redirects=True)
            close_client = True

        try:
            response = await client.get(connector.url, headers=headers)
        except httpx.TimeoutException:
            return FetchFailed(ErrorCategory.timeout, True, f"Timeout fetching {connector.url}")
        except httpx.ConnectError as e:
            return FetchFailed(ErrorCategory.connection, True, str(e))
        except httpx.HTTPError as e:
            return FetchFailed(ErrorCategory.connection, True, str(e))
        finally:
            if close_client:
                await client.aclose()

        if response.status_code == 304:
            return NotModified()

        if response.status_code >= 500:
            return FetchFailed(
                ErrorCategory.provider_internal, True, f"HTTP {response.status_code}",
            )
        if response.status_code == 429:
            return FetchFailed(ErrorCategory.rate_limit, True, "HTTP 429 rate limit")
        if response.status_code == 404:
            return FetchFailed(ErrorCategory.model_not_found, False, "HTTP 404 not found")
        if response.status_code >= 400:
            return FetchFailed(ErrorCategory.invalid_request, False, f"HTTP {response.status_code}")

        raw_body = response.content
        raw_hash = hashlib.sha256(raw_body).hexdigest()

        entries = self._parse_feed(raw_body, connector.connector_id)

        new_etag = response.headers.get("ETag", cursor.etag if cursor else "")
        new_last_modified = response.headers.get(
            "Last-Modified", cursor.last_modified if cursor else "",
        )

        return Fetched(
            entries=entries,
            new_etag=new_etag,
            new_last_modified=new_last_modified,
            raw_body=raw_body,
            raw_content_hash=raw_hash,
        )

    def _parse_feed(self, raw_body: bytes, connector_id: str) -> list[RssEntry]:
        """解析 feed XML 为 RssEntry 列表。"""
        try:
            import feedparser
        except ImportError:
            return _parse_feed_stdlib(raw_body)

        parsed = feedparser.parse(raw_body)
        entries: list[RssEntry] = []

        for entry in parsed.entries:
            title = _get_text(entry, "title", "")
            link = _get_text(entry, "link", "")
            summary = _get_text(entry, "summary", "") or _get_text(entry, "description", "")
            author = _get_text(entry, "author", "")

            # 稳定 ID：优先 entry.id，否则 link，否则 title+published
            source_id = _get_text(entry, "id", "")
            if not source_id:
                source_id = link
            if not source_id:
                source_id = title + "|" + _get_text(entry, "published", "")

            published_at = _parse_date(entry.get("published_parsed") or entry.get("updated_parsed"))

            content_hash = compute_content_hash(title, link, summary)

            entries.append(RssEntry(
                source_item_id=source_id,
                title=title,
                link=link,
                summary=summary,
                author=author,
                published_at=published_at,
                content_hash=content_hash,
            ))

        return entries


def _parse_feed_stdlib(raw_body: bytes) -> list[RssEntry]:
    """Dependency-free RSS/Atom fallback used in minimal/offline installs."""
    from email.utils import parsedate_to_datetime
    from xml.etree import ElementTree

    root = ElementTree.fromstring(raw_body)

    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    nodes = [
        node for node in root.iter()
        if local_name(node.tag) in ("item", "entry")
    ]
    results: list[RssEntry] = []
    for node in nodes:
        values: dict[str, str] = {}
        for child in node:
            name = local_name(child.tag)
            text = "".join(child.itertext()).strip()
            if name == "link" and not text:
                text = child.attrib.get("href", "")
            if text and name not in values:
                values[name] = text
        title = values.get("title", "")
        link = values.get("link", "")
        summary = values.get("summary") or values.get("description", "")
        author = values.get("author", "")
        source_id = values.get("guid") or values.get("id") or link
        published_raw = (
            values.get("pubDate")
            or values.get("published")
            or values.get("updated", "")
        )
        if not source_id:
            source_id = f"{title}|{published_raw}"
        published_at = None
        if published_raw:
            try:
                published_at = parsedate_to_datetime(published_raw)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)
                else:
                    published_at = published_at.astimezone(UTC)
            except (TypeError, ValueError):
                try:
                    published_at = datetime.fromisoformat(
                        published_raw.replace("Z", "+00:00")
                    ).astimezone(UTC)
                except ValueError:
                    published_at = None
        results.append(RssEntry(
            source_item_id=source_id,
            title=title,
            link=link,
            summary=summary,
            author=author,
            published_at=published_at,
            content_hash=compute_content_hash(title, link, summary),
        ))
    return results


def _get_text(entry: Any, key: str, default: str = "") -> str:
    """安全获取 feedparser entry 文本字段。"""
    val = entry.get(key)
    if isinstance(val, str):
        return val
    if hasattr(val, "value"):
        return val.value
    return default or ""


def _parse_date(struct_time) -> datetime | None:
    """将 time.struct_time 转为 UTC datetime。"""
    if struct_time is None:
        return None
    try:
        import calendar
        ts = calendar.timegm(struct_time)
        return datetime.fromtimestamp(ts, tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None
