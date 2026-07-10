"""Digest / DigestItem 数据访问层。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from cogito.domain.digest import Digest, DigestStatus
from cogito.contracts.clock import epoch_ms, from_epoch_ms


class DigestRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, digest: Digest) -> None:
        self._conn.execute(
            "INSERT INTO digests (digest_id, principal_id, digest_date, status, "
            "  item_count, content_ref, created_at, rendered_at) "
            "VALUES (?,?,?,?,?, ?,?,?)",
            (
                digest.digest_id,
                digest.principal_id,
                digest.digest_date,
                digest.status.value,
                digest.item_count,
                digest.content_ref,
                epoch_ms(digest.created_at),
                epoch_ms(digest.rendered_at),
            ),
        )

    def add_item(self, digest_id: str, item_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO digest_items (digest_id, item_id) VALUES (?,?)",
            (digest_id, item_id),
        )

    def find_by_date(self, principal_id: str, digest_date: str) -> Digest | None:
        row = self._conn.execute(
            "SELECT * FROM digests WHERE principal_id=? AND digest_date=? "
            "ORDER BY created_at DESC LIMIT 1",
            (principal_id, digest_date),
        ).fetchone()
        return self._row_to_digest(row) if row else None

    def find_latest(self, principal_id: str) -> Digest | None:
        row = self._conn.execute(
            "SELECT * FROM digests WHERE principal_id=? "
            "ORDER BY digest_date DESC LIMIT 1",
            (principal_id,),
        ).fetchone()
        return self._row_to_digest(row) if row else None

    def find_all(self, principal_id: str, limit: int = 30) -> list[Digest]:
        rows = self._conn.execute(
            "SELECT * FROM digests WHERE principal_id=? "
            "ORDER BY digest_date DESC LIMIT ?",
            (principal_id, limit),
        ).fetchall()
        return [self._row_to_digest(r) for r in rows]

    def update_status(self, digest_id: str, status: DigestStatus) -> None:
        self._conn.execute(
            "UPDATE digests SET status=? WHERE digest_id=?",
            (status.value, digest_id),
        )

    def set_rendered(self, digest_id: str, content_ref: str) -> None:
        self._conn.execute(
            "UPDATE digests SET content_ref=?, rendered_at=?, status='ready' "
            "WHERE digest_id=?",
            (content_ref, epoch_ms(datetime.now(UTC)), digest_id),
        )

    def get_items(self, digest_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT item_id FROM digest_items WHERE digest_id=?",
            (digest_id,),
        ).fetchall()
        return [r["item_id"] for r in rows]

    @staticmethod
    def _row_to_digest(row: sqlite3.Row) -> Digest:
        return Digest(
            digest_id=row["digest_id"],
            principal_id=row["principal_id"],
            digest_date=row["digest_date"],
            status=row["status"],
            item_count=row["item_count"],
            content_ref=row["content_ref"],
            created_at=from_epoch_ms(row["created_at"]),
            rendered_at=from_epoch_ms(row["rendered_at"]),
        )
