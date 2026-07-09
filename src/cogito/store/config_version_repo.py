"""ConfigVersionRepository —— config_versions 表数据访问（Plan 06 M2）。

每次启动/热更新插入一条，Attempt/Task/Decision 可追溯使用的 config hash。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass
class ConfigVersionRecord:
    version_id: str
    content_hash: str
    schema_version: str
    source_layers: list[str]
    applied_at: int
    applied_by: str | None = None
    change_summary: str | None = None


class ConfigVersionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ConfigVersionRecord) -> None:
        self._conn.execute(
            "INSERT INTO config_versions (version_id, content_hash, schema_version, "
            "source_layers, applied_at, applied_by, change_summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record.version_id, record.content_hash, record.schema_version,
             json.dumps(record.source_layers), record.applied_at,
             record.applied_by, record.change_summary),
        )

    def get_by_hash(self, content_hash: str) -> ConfigVersionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM config_versions WHERE content_hash=?", (content_hash,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get(self, version_id: str) -> ConfigVersionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM config_versions WHERE version_id=?", (version_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def latest(self) -> ConfigVersionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM config_versions ORDER BY applied_at DESC LIMIT 1",
        ).fetchone()
        return self._row_to_record(row) if row else None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ConfigVersionRecord:
        return ConfigVersionRecord(
            version_id=row["version_id"],
            content_hash=row["content_hash"],
            schema_version=row["schema_version"],
            source_layers=json.loads(row["source_layers"]) if row["source_layers"] else [],
            applied_at=row["applied_at"],
            applied_by=row["applied_by"],
            change_summary=row["change_summary"],
        )
