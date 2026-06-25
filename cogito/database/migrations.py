"""
cogito.database.migrations — Schema 版本迁移

使用 PRAGMA user_version 进行版本管理。
版本号递增，每个版本对应一个迁移函数。

迁移流程：
    1. 读取当前 user_version
    2. 按顺序执行 > 当前版本的迁移函数
    3. 每执行成功一个，更新 user_version
"""

from __future__ import annotations

import logging

from pathlib import Path

from cogito.database.connection import AsyncDatabase
from cogito.database.schema import (
    SCHEMA_VERSION,
    get_ddl_statements,
)

logger = logging.getLogger(__name__)


async def get_current_version(db: AsyncDatabase) -> int:
    """读取当前数据库 schema 版本。"""
    row = await db.fetchone("PRAGMA user_version;")
    return row.get("user_version", 0) if row else 0


async def set_version(db: AsyncDatabase, version: int) -> None:
    """设置数据库 schema 版本。"""
    await db.execute(f"PRAGMA user_version = {version};")


async def run_migrations(db: AsyncDatabase) -> int:
    """执行所有待执行的迁移，返回当前版本号。"""
    current = await get_current_version(db)

    if current == 0:
        # 全新数据库 — 执行完整 DDL (v1) + 后续所有迁移
        logger.info("Fresh database, applying schema v%s", SCHEMA_VERSION)
        for stmt in get_ddl_statements():
            await db.executescript(stmt)
        # Apply remaining migrations (v2, v3, ...)
        for version in range(1, SCHEMA_VERSION + 1):
            migrator = _MIGRATIONS.get(version)
            if migrator and version > 1:  # v1 already applied via get_ddl_statements()
                logger.info("Running migration v%s", version)
                await migrator(db)
        await set_version(db, SCHEMA_VERSION)
        logger.info("Schema v%s applied", SCHEMA_VERSION)
        return SCHEMA_VERSION

    if current < SCHEMA_VERSION:
        # 需要增量迁移
        for version in range(current + 1, SCHEMA_VERSION + 1):
            migrator = _MIGRATIONS.get(version)
            if migrator:
                logger.info("Running migration v%s → v%s", version - 1, version)
                await migrator(db)
                await set_version(db, version)
                logger.info("Migration v%s complete", version)
            else:
                await set_version(db, version)
        return SCHEMA_VERSION

    # 已是最新版本
    logger.info("Schema is up-to-date (v%s)", current)
    return current


async def migrate_v1(db: AsyncDatabase) -> None:
    """v0 → v1：初始建表。

    v1 是首个正式版本，包含完整的 3 张核心表 + FTS5 + 索引 + 触发器。
    如果 v0 时已通过 run_migrations 执行了完整 DDL，此函数仅在增量
    迁移路径上被调用（新数据库从 v0 直接跳转到 SCHEMA_VERSION）。
    """
    for stmt in get_ddl_statements():
        await db.executescript(stmt)


async def migrate_v2(db: AsyncDatabase) -> None:
    """v1 → v2：PersistencePhase 控制表。

    新增 4 张控制表（sessions、turn_commits、candidate_write_audits、
    embedding_jobs），并为已有 events 和 trace_events 表增加
    request_id/turn_id 列及关联索引。
    """
    sql_path = Path(__file__).parent / "migrations" / "002_persistence_control.sql"
    sql = sql_path.read_text(encoding="utf-8")
    await db.executescript(sql)


async def migrate_v3(db: AsyncDatabase) -> None:
    """v2 → v3：StateLoadPhase 控制表。

    新增 3 张只读控制表（user_profiles、user_settings、session_configs）。
    StateLoadPhase 从这些表加载确定性用户状态和会话配置。
    """
    sql_path = Path(__file__).parent / "migrations" / "003_state_load_tables.sql"
    sql = sql_path.read_text(encoding="utf-8")
    await db.executescript(sql)


async def migrate_v4(db: AsyncDatabase) -> None:
    """v3 → v4：为 sessions 表增加 title 列。

    sessions 表已在 v2 迁移中创建。v4 增加 title 列用于
    Web Channel 显示会话标题。如果列已存在则静默跳过。
    """
    try:
        await db.execute("ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        logger.info("Added title column to sessions table")
    except Exception:
        # Column may already exist — this is harmless
        logger.debug("title column already exists in sessions table")


# 注册迁移函数：version → migrator
_MIGRATIONS: dict[int, callable] = {
    1: migrate_v1,
    2: migrate_v2,
    3: migrate_v3,
    4: migrate_v4,
}
