"""Schema migration runner — discovers and applies numbered SQL migration files.

遵循 CONFIG-PROFILES / 1（配置层级）与 DATABASE-SCHEMA / 1（SQLite 模式）：
- 每个 Migration 是独立 SQL 文件，按版本号递增
- 文件命名：NNNN_description.sql（NNNN = 零填充版本号）
- 版本从 1 开始，无上限
- 支持空库从头应用到最新、已有库增量升级
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_VERSION_PATTERN = re.compile(r"^(\d+)_")


class MigrationFile(NamedTuple):
    version: int
    path: Path


def _discover() -> list[MigrationFile]:
    """扫描 migrations/ 目录，返回按版本升序的迁移列表。"""
    if not MIGRATIONS_DIR.is_dir():
        return []
    files: list[MigrationFile] = []
    for p in sorted(MIGRATIONS_DIR.iterdir()):
        if p.suffix != ".sql":
            continue
        m = _VERSION_PATTERN.match(p.name)
        if m:
            files.append(MigrationFile(version=int(m.group(1)), path=p))
    return files


def _get_current_version(conn: sqlite3.Connection) -> int:
    """查询已应用的最大 Migration 版本。"""
    try:
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        return row[0] if row and row[0] else 0
    except sqlite3.OperationalError:
        return 0


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    """确保 _schema_version 表存在（用于空库首次迁移）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version     INTEGER NOT NULL,
            applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            checksum    TEXT    NOT NULL DEFAULT ''
        )
    """)


def _apply_one(conn: sqlite3.Connection, mf: MigrationFile) -> None:
    """应用单个迁移文件并记录版本。"""
    sql = mf.path.read_text(encoding="utf-8")
    checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
    conn.executescript(sql)
    conn.execute(
        "INSERT INTO _schema_version (version, checksum) VALUES (?, ?)",
        (mf.version, checksum),
    )


def migrate(conn: sqlite3.Connection) -> None:
    """运行所有待处理的 Migration（自动发现 + 增量应用）。

    幂等保证：
    - 已应用的版本跳过
    - 同一版本重复执行：CREATE IF NOT EXISTS 保证幂等
    """
    _ensure_schema_version_table(conn)
    current = _get_current_version(conn)
    pending = [mf for mf in _discover() if mf.version > current]

    for mf in pending:
        _apply_one(conn, mf)

    if pending:
        conn.commit()
