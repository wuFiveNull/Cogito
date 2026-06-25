"""conftest for agent/tools tests — re-export database fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
async def db(temp_db_path):
    """Create a fresh SQLite database for testing."""
    from cogito.database.connection import AsyncDatabase
    from cogito.database.migrations import run_migrations

    db = AsyncDatabase(temp_db_path)
    await db.open()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
def temp_db_path():
    import os
    import tempfile
    tmp = tempfile.mktemp(suffix=".db")
    yield tmp
    try:
        os.unlink(tmp)
    except OSError:
        pass
