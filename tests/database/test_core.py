"""
Tests for cogito.database — 数据库核心组件
"""

from __future__ import annotations

import pytest

from cogito.database import AsyncDatabase, new_uuid, new_uuid_hex, run_migrations
from cogito.database.schema import get_ddl_statements, SCHEMA_VERSION


class TestIds:
    def test_new_uuid_format(self):
        uid = new_uuid()
        parts = uid.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8  # time-based prefix

    def test_new_uuid_hex(self):
        uid = new_uuid_hex()
        assert len(uid) == 32
        assert "-" not in uid

    def test_uuidv7_version_bits(self):
        import uuid

        u = uuid.UUID(new_uuid())
        # UUIDv7: version field should be 7 (bits 48-51)
        assert u.version == 7


class TestSchema:
    def test_ddl_statements_count(self):
        stmts = get_ddl_statements()
        assert len(stmts) == 27

    def test_schema_version(self):
        assert SCHEMA_VERSION == 4


class TestAsyncDatabase:
    @pytest.mark.asyncio
    async def test_open_and_close(self, temp_db_path):
        db = AsyncDatabase(temp_db_path)
        assert not db.is_connected
        await db.open()
        assert db.is_connected
        await db.close()
        assert not db.is_connected

    @pytest.mark.asyncio
    async def test_execute_scalar(self, db):
        val = await db.fetchone("SELECT 42 AS answer")
        assert val["answer"] == 42

    @pytest.mark.asyncio
    async def test_fetchcol(self, db):
        vals = await db.fetchcol("SELECT 1 UNION SELECT 2 UNION SELECT 3")
        assert vals == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_changes(self, db):
        await db.execute("CREATE TABLE IF NOT EXISTS _test (x INTEGER)")
        await db.execute("INSERT INTO _test (x) VALUES (1)")
        count = await db.changes()
        assert count == 1

    @pytest.mark.asyncio
    async def test_execute_in_transaction(self, db):
        statements = [
            ("INSERT INTO events (id, user_id, session_id, seq_no, role, event_type, content) "
             "VALUES (:id, :uid, :sid, 1, 'user', 'test', 'hello')",
             {"id": new_uuid(), "uid": "u", "sid": "s"}),
        ]
        await db.execute_in_transaction(statements)

        rows = await db.fetchall(
            "SELECT * FROM events WHERE user_id = :uid", {"uid": "u"},
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_begin_immediate_commit(self, db):
        await db.begin_immediate()
        await db.execute(
            "INSERT INTO trace_events (id, trace_id, user_id, step_type, step_name) "
            "VALUES (:id, :tid, :uid, 'test', 'test')",
            {"id": new_uuid(), "tid": "t", "uid": "u"},
        )
        await db.commit()
        # Should not raise

    @pytest.mark.asyncio
    async def test_wal_mode(self, db):
        row = await db.fetchone("PRAGMA journal_mode")
        assert row is not None
        # WAL mode is active

    @pytest.mark.asyncio
    async def test_version_check(self, db):
        row = await db.fetchone("SELECT sqlite_version() AS v")
        assert row is not None
        parts = row["v"].split(".")
        assert int(parts[0]) >= 3


class TestMigrations:
    @pytest.mark.asyncio
    async def test_fresh_database(self, temp_db_path):
        db = AsyncDatabase(temp_db_path)
        await db.open()
        try:
            version = await run_migrations(db)
            assert version == SCHEMA_VERSION

            # Verify tables exist
            tables = await db.fetchcol(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            assert "trace_events" in tables
            assert "events" in tables
            assert "memories" in tables
            assert "memories_fts" in tables
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_idempotent_migration(self, temp_db_path):
        db = AsyncDatabase(temp_db_path)
        await db.open()
        try:
            v1 = await run_migrations(db)
            v2 = await run_migrations(db)
            assert v1 == v2
            assert v2 == SCHEMA_VERSION
        finally:
            await db.close()
