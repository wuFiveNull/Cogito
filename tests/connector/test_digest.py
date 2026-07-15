"""Digest 模型 + 查询 + REPL 命令测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.connector import ConnectorItem, ItemStatus
from cogito.domain.digest import Digest, DigestStatus
from cogito.service.digest_service import DigestService
from cogito.store.connector_repo import ConnectorItemRepository
from cogito.store.digest_repo import DigestRepository


class TestDigestEntity:
    def test_round_trip(self):
        d = Digest(
            digest_id="d1",
            principal_id="owner",
            digest_date="2026-07-07",
            item_count=3,
        )
        assert d.status == DigestStatus.pending
        data = d.to_dict()
        assert data["digest_date"] == "2026-07-07"


class TestDigestRepository:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    def test_insert_and_find(self, conn):
        d = Digest(digest_id="d1", principal_id="owner", digest_date="2026-07-07", item_count=2)
        DigestRepository(conn).insert(d)
        got = DigestRepository(conn).find_by_date("owner", "2026-07-07")
        assert got is not None
        assert got.item_count == 2

    def test_add_items(self, conn):
        # 先建 connector + item 满足外键
        from cogito.domain.connector import Connector
        from cogito.store.connector_repo import ConnectorRepository

        ConnectorRepository(conn).insert(Connector(connector_id="c1", url="http://x"))
        conn.commit()
        self._insert_raw_item(conn, "item-a")
        self._insert_raw_item(conn, "item-b")

        d = Digest(digest_id="d1", principal_id="owner", digest_date="2026-07-07")
        repo = DigestRepository(conn)
        repo.insert(d)
        repo.add_item("d1", "item-a")
        repo.add_item("d1", "item-b")
        items = repo.get_items("d1")
        assert items == ["item-a", "item-b"]

    def test_add_item_idempotent(self, conn):
        from cogito.domain.connector import Connector
        from cogito.store.connector_repo import ConnectorRepository

        ConnectorRepository(conn).insert(Connector(connector_id="c1", url="http://x"))
        conn.commit()
        self._insert_raw_item(conn, "x")

        d = Digest(digest_id="d1", principal_id="owner", digest_date="2026-07-07")
        repo = DigestRepository(conn)
        repo.insert(d)
        repo.add_item("d1", "x")
        repo.add_item("d1", "x")  # 重复
        assert repo.get_items("d1") == ["x"]

    @staticmethod
    def _insert_raw_item(conn, item_id):
        from cogito.domain.connector import ConnectorItem

        ConnectorItemRepository(conn).insert(
            ConnectorItem(
                item_id=item_id,
                connector_id="c1",
                source_item_id=item_id,
                content_hash=f"h-{item_id}",
                status=ItemStatus.digest,
            )
        )
        conn.commit()

    def test_find_latest(self, conn):
        repo = DigestRepository(conn)
        repo.insert(Digest(digest_id="d1", principal_id="owner", digest_date="2026-07-06"))
        repo.insert(Digest(digest_id="d2", principal_id="owner", digest_date="2026-07-08"))
        latest = repo.find_latest("owner")
        assert latest.digest_date == "2026-07-08"

    def test_update_status(self, conn):
        d = Digest(digest_id="d1", principal_id="owner", digest_date="2026-07-07")
        repo = DigestRepository(conn)
        repo.insert(d)
        repo.update_status("d1", DigestStatus.ready)
        assert repo.find_by_date("owner", "2026-07-07").status == DigestStatus.ready

    def test_set_rendered(self, conn):
        d = Digest(digest_id="d1", principal_id="owner", digest_date="2026-07-07")
        repo = DigestRepository(conn)
        repo.insert(d)
        repo.set_rendered("d1", "payload:rendered")
        got = repo.find_by_date("owner", "2026-07-07")
        assert got.content_ref == "payload:rendered"
        assert got.status == DigestStatus.ready
        assert got.rendered_at is not None


class TestDigestService:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    def _make_item(
        self,
        conn,
        connector_id="c1",
        title="T",
        status=ItemStatus.digest,
        relevance=0.8,
        days_ago=0,
    ):
        # 确保 connector 存在
        from cogito.domain.connector import Connector
        from cogito.store.connector_repo import ConnectorRepository

        if ConnectorRepository(conn).get(connector_id) is None:
            ConnectorRepository(conn).insert(
                Connector(connector_id=connector_id, url="http://x"),
            )
            conn.commit()
        now = datetime.now(UTC) - timedelta(days=days_ago)
        item = ConnectorItem(
            connector_id=connector_id,
            source_item_id=f"src-{title}",
            title=title,
            link=f"http://x/{title}",
            content_hash=f"h-{title}",
            relevance=relevance,
            summary_text=f"摘要 {title}",
            status=status,
            created_at=now,
        )
        ConnectorItemRepository(conn).insert(item)
        return item

    def test_assemble_empty(self, conn):
        svc = DigestService(conn)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        result = svc.assemble_digest("owner", today)
        assert result is None

    def test_assemble_collects_digest_items(self, conn):
        self._make_item(conn, title="A", status=ItemStatus.digest, relevance=0.9)
        self._make_item(conn, title="B", status=ItemStatus.digest, relevance=0.5)
        self._make_item(conn, title="C", status=ItemStatus.silent, relevance=0.7)  # 不收集

        svc = DigestService(conn)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        digest = svc.assemble_digest("owner", today)
        assert digest is not None
        assert digest.item_count == 2  # 仅 digest 状态

    def test_assemble_view_ordered_by_relevance(self, conn):
        self._make_item(conn, title="Low", status=ItemStatus.digest, relevance=0.3)
        self._make_item(conn, title="High", status=ItemStatus.digest, relevance=0.95)

        svc = DigestService(conn)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        digest = svc.assemble_digest("owner", today)
        view = svc.get_digest_view(digest.digest_id)
        assert view["items"][0]["title"] == "High"  # 高相关度排前

    def test_assemble_idempotent_same_date(self, conn):
        self._make_item(conn, title="A", status=ItemStatus.digest)
        svc = DigestService(conn)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        d1 = svc.assemble_digest("owner", today)
        d2 = svc.assemble_digest("owner", today)
        assert d1.digest_id == d2.digest_id  # 复用同日

    def test_get_today_digest_none(self, conn):
        svc = DigestService(conn)
        assert svc.get_today_digest("owner") is None

    def test_list_digests(self, conn):
        repo = DigestRepository(conn)
        repo.insert(
            Digest(digest_id="d1", principal_id="owner", digest_date="2026-07-07", item_count=2)
        )
        repo.insert(
            Digest(digest_id="d2", principal_id="owner", digest_date="2026-07-08", item_count=5)
        )
        svc = DigestService(conn)
        listing = svc.list_digests("owner")
        assert len(listing) == 2
        assert listing[0]["digest_date"] == "2026-07-08"  # 倒序
