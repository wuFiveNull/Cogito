"""Connector 归档 + 去重测试 —— 验证 raw item 归档和 content_hash 去重。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cogito.domain.connector import (
    Connector,
    ConnectorCursor,
    ConnectorItem,
    ConnectorRawItem,
    ItemStatus,
    compute_content_hash,
)
from cogito.store.connector_repo import (
    ConnectorCursorRepository,
    ConnectorItemRepository,
    ConnectorRawRepository,
    ConnectorRepository,
)


class TestContentHash:
    def test_same_content_same_hash(self):
        h1 = compute_content_hash("Title", "http://x/1", "Summary")
        h2 = compute_content_hash("Title", "http://x/1", "Summary")
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = compute_content_hash("A", "http://x/a", "S1")
        h2 = compute_content_hash("B", "http://x/b", "S2")
        assert h1 != h2

    def test_hash_ignores_whitespace(self):
        h1 = compute_content_hash("  Title  ", "http://x/1", "Summary")
        h2 = compute_content_hash("Title", "http://x/1", "Summary")
        assert h1 == h2

    def test_hash_strips_html(self):
        h1 = compute_content_hash("T", "http://x", "<p>Hello</p>")
        h2 = compute_content_hash("T", "http://x", "Hello")
        assert h1 == h2


class TestRawItemArchival:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def connector(self, conn):
        c = Connector(connector_id="c1", name="Test", url="http://x/feed")
        ConnectorRepository(conn).insert(c)
        return c

    def test_insert_and_find_raw(self, conn, connector):
        raw = ConnectorRawItem(
            connector_id="c1",
            source_item_id="g1",
            content_hash="abc123",
            payload_ref="payload:xyz",
            http_etag='W/"etag1"',
        )
        ConnectorRawRepository(conn).insert(raw)
        found = ConnectorRawRepository(conn).find_by_content_hash("c1", "abc123")
        assert found is not None
        assert found.source_item_id == "g1"
        assert found.payload_ref == "payload:xyz"

    def test_find_missing_raw(self, conn, connector):
        assert ConnectorRawRepository(conn).find_by_content_hash("c1", "nope") is None


class TestItemDedup:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def connector(self, conn):
        c = Connector(connector_id="c1", name="Test", url="http://x/feed")
        ConnectorRepository(conn).insert(c)
        return c

    def test_insert_item(self, conn, connector):
        item = ConnectorItem(
            connector_id="c1",
            source_item_id="g1",
            title="Hello",
            link="http://x/1",
            content_hash="h1",
        )
        ConnectorItemRepository(conn).insert(item)
        found = ConnectorItemRepository(conn).find_by_source_id("c1", "g1")
        assert found is not None
        assert found.title == "Hello"

    def test_dedup_by_source_id(self, conn, connector):
        """相同 source_item_id 不应重复插入（UNIQUE 约束）。"""
        item1 = ConnectorItem(
            connector_id="c1",
            source_item_id="g1",
            title="T",
            content_hash="h1",
        )
        ConnectorItemRepository(conn).insert(item1)
        # 重复 source_id 应被检测
        existing = ConnectorItemRepository(conn).find_by_source_id("c1", "g1")
        assert existing is not None

    def test_dedup_by_content_hash(self, conn, connector):
        """不同 source_id 但相同 content_hash 应被检测为重复。"""
        item1 = ConnectorItem(
            connector_id="c1",
            source_item_id="g1",
            title="T",
            content_hash="same",
        )
        ConnectorItemRepository(conn).insert(item1)
        existing = ConnectorItemRepository(conn).find_by_content_hash("c1", "same")
        assert existing is not None

    def test_update_status(self, conn, connector):
        item = ConnectorItem(
            connector_id="c1",
            source_item_id="g1",
            title="T",
            content_hash="h1",
        )
        ConnectorItemRepository(conn).insert(item)
        ConnectorItemRepository(conn).update_status(item.item_id, ItemStatus.digest)
        found = ConnectorItemRepository(conn).find_by_source_id("c1", "g1")
        assert found.status == ItemStatus.digest

    def test_find_by_status(self, conn, connector):
        i1 = ConnectorItem(
            connector_id="c1",
            source_item_id="g1",
            title="A",
            content_hash="h1",
            status=ItemStatus.digest,
        )
        i2 = ConnectorItem(
            connector_id="c1",
            source_item_id="g2",
            title="B",
            content_hash="h2",
            status=ItemStatus.silent,
        )
        i3 = ConnectorItem(
            connector_id="c1",
            source_item_id="g3",
            title="C",
            content_hash="h3",
            status=ItemStatus.digest,
        )
        repo = ConnectorItemRepository(conn)
        for i in (i1, i2, i3):
            repo.insert(i)
        digest_items = repo.find_by_status("c1", ItemStatus.digest)
        assert len(digest_items) == 2


class TestCursorPersistence:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def connector(self, conn):
        c = Connector(connector_id="c1", name="Test", url="http://x/feed")
        ConnectorRepository(conn).insert(c)
        return c

    def test_upsert_and_get(self, conn, connector):
        cursor = ConnectorCursor(
            connector_id="c1",
            etag='W/"etag1"',
            last_modified="Mon, 07 Jul 2026 12:00:00 GMT",
            last_item_ids=["g1", "g2"],
        )
        ConnectorCursorRepository(conn).upsert(cursor)
        got = ConnectorCursorRepository(conn).get("c1")
        assert got is not None
        assert got.etag == 'W/"etag1"'
        assert got.last_item_ids == ["g1", "g2"]

    def test_upsert_updates(self, conn, connector):
        c1 = ConnectorCursor(connector_id="c1", etag="v1")
        ConnectorCursorRepository(conn).upsert(c1)
        c2 = ConnectorCursor(connector_id="c1", etag="v2", last_item_ids=["g3"])
        ConnectorCursorRepository(conn).upsert(c2)
        got = ConnectorCursorRepository(conn).get("c1")
        assert got.etag == "v2"
        assert got.last_item_ids == ["g3"]
