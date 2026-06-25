"""验证 Phase 1 实现的快速测试脚本"""

import asyncio
import os
import tempfile

from cogito.database import AsyncDatabase, new_uuid, run_migrations
from cogito.database.schema import get_ddl_statements


async def test():
    # 1. UUIDv7
    uid = new_uuid()
    print(f"UUIDv7: {uid}")
    print(f"UUIDv7 length: {len(uid)}")

    # 2. DDL
    stmts = get_ddl_statements()
    print(f"DDL statements: {len(stmts)}")

    # 3. 创建临时数据库
    tmp = tempfile.mktemp(suffix=".db")
    db = AsyncDatabase(tmp)
    try:
        await db.open()
        version = await run_migrations(db)
        print(f"Schema version: {version}")

        tables = await db.fetchcol(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        print(f"Tables: {tables}")

        fts = await db.fetchcol(
            "SELECT name FROM sqlite_master WHERE type='virtual_table'"
        )
        print(f"FTS: {fts}")

        triggers = await db.fetchcol(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
        )
        print(f"Triggers: {triggers}")

        indexes = await db.fetchcol(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        print(f"Indexes: {indexes}")

        sqlite_version = await db.fetchone("SELECT sqlite_version() as v")
        print(f"SQLite version: {sqlite_version['v']}")

        # 验证 WAL 模式
        journal = await db.fetchone("PRAGMA journal_mode")
        print(f"Journal mode: {journal}")

        print("\n** Phase 1 passed! **")
    finally:
        await db.close()
        os.unlink(tmp)


asyncio.run(test())
