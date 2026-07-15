"""DigestService —— 聚合当日 digest 状态条目，提供查询视图。"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from cogito.contracts.clock import epoch_ms
from cogito.domain.digest import Digest, DigestStatus
from cogito.store.digest_repo import DigestRepository

_LOGGER = logging.getLogger(__name__)


class DigestService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def assemble_digest(
        self,
        principal_id: str,
        digest_date: str,
    ) -> Digest | None:
        """将当日 status='digest' 的条目聚合为一个 Digest。"""
        digest_repo = DigestRepository(self._conn)

        # 跨 connector 收集当日 digest 条目（created_at 在当日)
        day_start = datetime.fromisoformat(f"{digest_date}T00:00:00+00:00")
        day_end = datetime.fromisoformat(f"{digest_date}T23:59:59+00:00")
        day_start_ms = epoch_ms(day_start)
        day_end_ms = epoch_ms(day_end)

        rows = self._conn.execute(
            "SELECT item_id FROM connector_items "
            "WHERE status='digest' "
            "AND created_at >= ? AND created_at <= ? "
            "ORDER BY relevance DESC NULLS LAST, created_at DESC",
            (day_start_ms, day_end_ms),
        ).fetchall()

        item_ids = [r["item_id"] for r in rows]
        if not item_ids:
            return None

        # 幂等：同日已存在则复用
        existing = digest_repo.find_by_date(principal_id, digest_date)
        if existing is not None:
            # 仅补充新增条目
            for iid in item_ids:
                digest_repo.add_item(existing.digest_id, iid)
            existing.item_count = len(digest_repo.get_items(existing.digest_id))
            return existing

        digest = Digest(
            principal_id=principal_id,
            digest_date=digest_date,
            status=DigestStatus.pending,
            item_count=len(item_ids),
        )
        digest_repo.insert(digest)
        for iid in item_ids:
            digest_repo.add_item(digest.digest_id, iid)

        _LOGGER.info(
            "DigestService: assembled %s with %d items",
            digest.digest_id,
            len(item_ids),
        )
        return digest

    def get_digest_view(self, digest_id: str) -> dict | None:
        """获取摘要视图（供 REPL 展示）。"""
        conn = self._conn
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM digests WHERE digest_id=?",
            (digest_id,),
        ).fetchone()
        if row is None:
            return None

        # 按 relevance 降序拉取（与 assemble 顺序一致）
        rows = conn.execute(
            "SELECT ci.item_id, ci.title, ci.link, ci.summary_text, ci.relevance "
            "FROM digest_items di "
            "JOIN connector_items ci ON ci.item_id = di.item_id "
            "WHERE di.digest_id=? "
            "ORDER BY ci.relevance DESC NULLS LAST, di.rowid ASC",
            (digest_id,),
        ).fetchall()
        items = [
            {
                "item_id": r["item_id"],
                "title": r["title"],
                "link": r["link"],
                "summary_text": r["summary_text"],
                "relevance": r["relevance"],
            }
            for r in rows
        ]

        return {
            "digest_id": row["digest_id"],
            "digest_date": row["digest_date"],
            "status": row["status"],
            "item_count": row["item_count"],
            "items": items,
        }

    def get_today_digest(self, principal_id: str = "owner") -> dict | None:
        """获取今日摘要视图（如不存在则尝试组装）。"""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        repo = DigestRepository(self._conn)
        digest = repo.find_by_date(principal_id, today)
        if digest is None:
            # 尝试组装
            digest = self.assemble_digest(principal_id, today)
        if digest is None:
            return None
        return self.get_digest_view(digest.digest_id)

    def list_digests(self, principal_id: str = "owner", limit: int = 14) -> list[dict]:
        """列出最近摘要。"""
        repo = DigestRepository(self._conn)
        digests = repo.find_all(principal_id, limit=limit)
        return [
            {
                "digest_id": d.digest_id,
                "digest_date": d.digest_date,
                "status": d.status.value,
                "item_count": d.item_count,
            }
            for d in digests
        ]
