"""Pytest fixtures for Cogito tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator

import pytest

from cogito.store.migration import migrate


@pytest.fixture
def in_memory_db() -> Generator[sqlite3.Connection, None, None]:
    """Create an in-memory SQLite database with full schema applied."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def empty_db() -> Generator[sqlite3.Connection, None, None]:
    """Create an in-memory SQLite database WITHOUT any schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def sample_principal() -> dict:
    return {
        "principal_id": "p1",
        "principal_type": "owner",
        "status": "active",
        "created_at": "2026-01-01T00:00:00+00:00",
        "metadata": '{"name": "test-owner"}',
    }
