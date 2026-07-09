"""Backfill —— 分批、可重入、有进度 Checkpoint 的回填器（Plan 06 M6）。

用于大型/破坏性 Migration 的数据回填，支持中断恢复。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

_BACKFILL_TABLE = """
CREATE TABLE IF NOT EXISTS _backfill_progress (
    migration_version INTEGER NOT NULL,
    last_key          TEXT,
    rows_processed    INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (migration_version)
)
"""


class Backfill:
    """分批回填器。

    按主键分页执行 transform_fn(row)，每批写 Checkpoint。
    中断后重新运行从最后 Checkpoint 继续。
    """

    def __init__(self, conn: sqlite3.Connection, batch_size: int = 1000) -> None:
        self._conn = conn
        self._batch_size = batch_size
        self._conn.execute(_BACKFILL_TABLE)

    def run(
        self,
        migration_version: int,
        table: str,
        transform_fn: Callable[[sqlite3.Row], dict[str, Any] | None],
        key_column: str = "rowid",
        update_sql: str | None = None,
    ) -> int:
        """执行回填。

        Args:
            migration_version: 关联的 migration 版本号。
            table: 要回填的表名。
            transform_fn: 转换函数，接收 row，返回要 UPDATE 的字段 dict，
                         返回 None 表示跳过该行。
            key_column: 用于分页的主键列名。
            update_sql: 自定义 UPDATE 语句（使用 :field 占位符）。
                        为 None 时根据 transform_fn 返回的字段自动生成。

        Returns:
            本次处理的行数。
        """
        # 读取进度
        progress = self._get_progress(migration_version)
        last_key = progress["last_key"] if progress else None
        rows_processed = progress["rows_processed"] if progress else 0

        total_this_run = 0
        while True:
            if last_key is None:
                rows = self._conn.execute(
                    f"SELECT * FROM {table} ORDER BY {key_column} ASC LIMIT ?",
                    (self._batch_size,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT * FROM {table} WHERE {key_column} > ? "
                    f"ORDER BY {key_column} ASC LIMIT ?",
                    (last_key, self._batch_size),
                ).fetchall()

            if not rows:
                break

            for row in rows:
                updates = transform_fn(row)
                if updates is None:
                    continue
                if update_sql:
                    self._conn.execute(update_sql, updates)
                else:
                    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
                    self._conn.execute(
                        f"UPDATE {table} SET {set_clause} WHERE {key_column} = :{key_column}",
                        {**updates, key_column: row[key_column]},
                    )
                last_key = row[key_column]
                rows_processed += 1
                total_this_run += 1

            # 每批写 Checkpoint
            self._save_progress(migration_version, last_key, rows_processed)
            self._conn.commit()

        return total_this_run

    def reset(self, migration_version: int) -> None:
        """重置回填进度（用于重新回填）。"""
        self._conn.execute(
            "DELETE FROM _backfill_progress WHERE migration_version=?",
            (migration_version,),
        )
        self._conn.commit()

    def _get_progress(self, migration_version: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT last_key, rows_processed FROM _backfill_progress "
            "WHERE migration_version=?",
            (migration_version,),
        ).fetchone()
        if row is None:
            return None
        return {"last_key": row[0], "rows_processed": row[1]}

    def _save_progress(
        self, migration_version: int, last_key: str, rows_processed: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO _backfill_progress "
            "(migration_version, last_key, rows_processed) VALUES (?, ?, ?) "
            "ON CONFLICT(migration_version) DO UPDATE SET "
            "last_key=excluded.last_key, rows_processed=excluded.rows_processed",
            (migration_version, last_key, rows_processed),
        )
