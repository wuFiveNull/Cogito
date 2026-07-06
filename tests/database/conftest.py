"""
conftest for database tests — 共享 Fixtures
"""

from __future__ import annotations

import os
import tempfile

import pytest

from cogito.database import AsyncDatabase, run_migrations
from cogito.database.manager import DatabaseManager
from cogito.database.ids import new_uuid


@pytest.fixture
def temp_db_path():
    """创建一个临时数据库路径。"""
    tmp = tempfile.mktemp(suffix=".db")
    yield tmp
    try:
        os.unlink(tmp)
    except OSError:
        pass


@pytest.fixture
async def db(temp_db_path):
    """创建一个已迁移的空的 AsyncDatabase 实例。"""
    db = AsyncDatabase(temp_db_path)
    await db.open()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
async def manager(temp_db_path):
    """创建一个 DatabaseManager 实例（已打开）。"""
    mgr = DatabaseManager(temp_db_path)
    await mgr.open()
    yield mgr
    await mgr.close()


@pytest.fixture
def user_id() -> str:
    return "test-user"


@pytest.fixture
def session_id() -> str:
    return "test-session"


@pytest.fixture
def trace_id() -> str:
    return new_uuid()
