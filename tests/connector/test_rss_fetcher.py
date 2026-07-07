"""RssFetcher 测试 —— 抓取、ETag/Last-Modified、故障注入。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from cogito.domain.connector import Connector, ConnectorCursor
from cogito.model.contracts import ErrorCategory
from cogito.service.rss_fetcher import (
    FetchFailed,
    Fetched,
    NotModified,
    RssFetcher,
)


def _make_connector(url: str, timeout: int = 5) -> Connector:
    return Connector(
        connector_id="c1",
        name="Test Feed",
        url=url,
        fetch_timeout_s=timeout,
    )


class TestRssFetcher:
    @pytest.fixture
    def fetcher(self):
        return RssFetcher()

    @pytest.mark.asyncio
    async def test_fetch_returns_entries(self, fetcher, fake_rss_server):
        fake_rss_server.set_entries([
            {"title": "Hello World", "link": "http://x/1", "description": "First post", "guid": "g1"},
            {"title": "Second", "link": "http://x/2", "description": "More", "guid": "g2"},
        ])
        conn = _make_connector(fake_rss_server.url)
        result = await fetcher.fetch(conn, None)

        assert isinstance(result, Fetched)
        assert len(result.entries) == 2
        assert result.entries[0].title == "Hello World"
        assert result.entries[0].source_item_id == "g1"
        assert result.entries[0].content_hash
        assert result.new_etag

    @pytest.mark.asyncio
    async def test_fetch_304_when_etag_matches(self, fetcher, fake_rss_server):
        fake_rss_server.set_entries([
            {"title": "A", "link": "http://x/a", "description": "", "guid": "ga"},
        ])
        conn = _make_connector(fake_rss_server.url)

        # 第一次：拿到 ETag
        first = await fetcher.fetch(conn, None)
        assert isinstance(first, Fetched)
        etag = first.new_etag
        assert etag

        # 第二次：带 ETag → 304
        cursor = ConnectorCursor(connector_id="c1", etag=etag)
        second = await fetcher.fetch(conn, cursor)
        assert isinstance(second, NotModified)

    @pytest.mark.asyncio
    async def test_fetch_etag_sent_in_request(self, fetcher, fake_rss_server):
        fake_rss_server.set_entries([{"title": "T", "link": "http://x/t", "description": "", "guid": "gt"}])
        conn = _make_connector(fake_rss_server.url)
        first = await fetcher.fetch(conn, None)
        etag = first.new_etag

        cursor = ConnectorCursor(connector_id="c1", etag=etag)
        await fetcher.fetch(conn, cursor)

        # 验证第二次请求带 If-None-Match
        assert fake_rss_server.etag_values == [etag]

    @pytest.mark.asyncio
    async def test_fetch_timeout(self, fetcher, fake_rss_server):
        fake_rss_server.set_entries([{"title": "T", "link": "http://x/t", "description": "", "guid": "gt"}])
        fake_rss_server.set_next_timeout(2.0)  # 2s 超时
        conn = _make_connector(fake_rss_server.url, timeout=1)

        result = await fetcher.fetch(conn, None)
        assert isinstance(result, FetchFailed)
        assert result.error_category == ErrorCategory.timeout
        assert result.retryable is True

    @pytest.mark.asyncio
    async def test_fetch_500_error(self, fetcher, fake_rss_server):
        fake_rss_server.set_next_status(500)
        conn = _make_connector(fake_rss_server.url)
        result = await fetcher.fetch(conn, None)
        assert isinstance(result, FetchFailed)
        assert result.error_category == ErrorCategory.provider_internal
        assert result.retryable is True

    @pytest.mark.asyncio
    async def test_fetch_404_not_retryable(self, fetcher, fake_rss_server):
        fake_rss_server.set_next_status(404)
        conn = _make_connector(fake_rss_server.url)
        result = await fetcher.fetch(conn, None)
        assert isinstance(result, FetchFailed)
        assert result.retryable is False

    @pytest.mark.asyncio
    async def test_fetch_429_rate_limit(self, fetcher, fake_rss_server):
        fake_rss_server.set_next_status(429)
        conn = _make_connector(fake_rss_server.url)
        result = await fetcher.fetch(conn, None)
        assert isinstance(result, FetchFailed)
        assert result.error_category == ErrorCategory.rate_limit
        assert result.retryable is True

    @pytest.mark.asyncio
    async def test_fetch_uses_link_when_no_guid(self, fetcher, fake_rss_server):
        fake_rss_server.set_entries([
            {"title": "No GUID", "link": "http://x/noguid", "description": ""},
        ])
        conn = _make_connector(fake_rss_server.url)
        result = await fetcher.fetch(conn, None)
        assert isinstance(result, Fetched)
        # 无 guid 时 source_item_id 回退到 link
        assert result.entries[0].source_item_id == "http://x/noguid"

    @pytest.mark.asyncio
    async def test_fetch_empty_feed(self, fetcher, fake_rss_server):
        fake_rss_server.set_entries([])
        conn = _make_connector(fake_rss_server.url)
        result = await fetcher.fetch(conn, None)
        assert isinstance(result, Fetched)
        assert result.entries == []
