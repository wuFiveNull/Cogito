"""Tests for SQLite connection manager."""

import os
import tempfile

from cogito.store.connection import get_connection, ConnectionPool


class TestGetConnection:
    def test_creates_db_file(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = get_connection(db_path)
            assert conn is not None
            # Should be WAL mode
            row = conn.execute("PRAGMA journal_mode").fetchone()
            assert row is not None
            conn.close()
        finally:
            os.unlink(db_path)

    def test_in_memory_works(self):
        conn = get_connection(":memory:")
        assert conn.execute("SELECT 1").fetchone() is not None
        conn.close()

    def test_foreign_keys_enabled(self):
        conn = get_connection(":memory:")
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row is not None
        assert row[0] == 1
        conn.close()


class TestConnectionPool:
    def test_get_returns_same_connection(self):
        pool = ConnectionPool(":memory:")
        c1 = pool.get()
        c2 = pool.get()
        assert c1 is c2
        pool.close()

    def test_close(self):
        pool = ConnectionPool(":memory:")
        pool.get()
        pool.close()
        assert pool._conn is None
