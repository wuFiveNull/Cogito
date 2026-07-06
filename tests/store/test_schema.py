"""Tests for SQLite schema and migration."""

import pytest


class TestMigration:
    def test_migrate_creates_tables(self, in_memory_db):
        """After migration, all core tables should exist."""
        tables = [
            "principals", "endpoints", "conversations", "sessions",
            "messages", "content_parts", "inbound_inbox",
            "turns", "run_attempts", "turn_checkpoints",
            "tasks", "task_attempts",
            "deliveries", "delivery_attempts",
            "tool_calls", "approvals",
            "memory_items",
            "payload_objects", "audit_records",
            "_schema_version",
        ]
        existing = {
            row[0]
            for row in in_memory_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for tbl in tables:
            assert tbl in existing, f"Missing table: {tbl}"

    def test_migrate_records_version(self, in_memory_db):
        row = in_memory_db.execute(
            "SELECT version FROM _schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row[0] >= 1

    def test_idempotent_migration(self, empty_db):
        """Running migrate twice should be safe."""
        from cogito.store.migration import migrate
        migrate(empty_db)
        migrate(empty_db)  # second run
        rows = empty_db.execute("SELECT version FROM _schema_version ORDER BY version").fetchall()
        versions = [r[0] for r in rows]
        assert versions == [1, 2]  # both migration versions applied exactly once

    def test_unique_constraints(self, in_memory_db):
        """Test a sample unique constraint."""
        in_memory_db.execute(
            "INSERT INTO principals (principal_id, principal_type, status, created_at, metadata) "
            "VALUES ('p1', 'owner', 'active', '2026-01-01T00:00:00Z', '{}')"
        )
        with pytest.raises(Exception):
            in_memory_db.execute(
                "INSERT INTO principals (principal_id, principal_type, status, created_at, metadata) "
                "VALUES ('p1', 'owner', 'active', '2026-01-01T00:00:00Z', '{}')"
            )

    def test_foreign_key_enforced(self, in_memory_db):
        """Inserting with invalid FK should fail."""
        with pytest.raises(Exception):
            in_memory_db.execute(
                "INSERT INTO endpoints (endpoint_id, principal_id) VALUES ('ep1', 'nonexistent')"
            )


class TestMigrationUpgrade:
    """Test v1 → v2 upgrade path."""

    def test_v1_upgrade_preserves_turn_data(self, empty_db):
        """Apply only v1 migration, insert data, then upgrade to v2."""
        from pathlib import Path
        import sqlite3
        from cogito.store.migration import migrate, _ensure_schema_version_table, MIGRATIONS_DIR

        # Apply v1 manually
        v1_sql = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
        _ensure_schema_version_table(empty_db)
        empty_db.executescript(v1_sql)
        empty_db.execute("INSERT INTO _schema_version (version, checksum) VALUES (1, 'v1')")
        empty_db.commit()

        # Insert data with old 'created' status
        empty_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) VALUES ('t1', 's1', 'created', '2026-01-01T00:00:00Z')"
        )
        empty_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) VALUES ('t2', 's2', 'running', '2026-01-01T00:00:00Z')"
        )
        empty_db.commit()

        # Run full migration (applies v2 upgrade)
        migrate(empty_db)

        # Verify data survived: 'created' → 'accepted'
        rows = empty_db.execute(
            "SELECT turn_id, status FROM turns ORDER BY turn_id"
        ).fetchall()
        assert [(r["turn_id"], r["status"]) for r in rows] == [
            ("t1", "accepted"),
            ("t2", "running"),
        ]

        # Verify new columns exist
        t1 = empty_db.execute(
            "SELECT input_message_id, version FROM turns WHERE turn_id = 't1'"
        ).fetchone()
        assert t1["input_message_id"] == ""
        assert t1["version"] == 1

    def test_version_recorded_after_upgrade(self, empty_db):
        """v1 → v2 upgrade should record both versions."""
        from cogito.store.migration import migrate, _ensure_schema_version_table, MIGRATIONS_DIR

        # Apply v1 manually
        v1_sql = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
        _ensure_schema_version_table(empty_db)
        empty_db.executescript(v1_sql)
        empty_db.execute("INSERT INTO _schema_version (version, checksum) VALUES (1, 'v1')")
        empty_db.commit()

        # Upgrade to v2
        migrate(empty_db)

        versions = {
            r[0] for r in empty_db.execute(
                "SELECT version FROM _schema_version"
            ).fetchall()
        }
        assert versions == {1, 2}

    def test_fresh_install_versions(self, empty_db):
        """Fresh migration from scratch applies all versions."""
        from cogito.store.migration import migrate

        migrate(empty_db)
        versions = {
            r[0] for r in empty_db.execute(
                "SELECT version FROM _schema_version"
            ).fetchall()
        }
        assert versions == {1, 2}

    def test_turn_schema_v2(self, in_memory_db):
        """Verify v2 schema constraints."""
        # Insert a turn with accepted status (v2 default)
        in_memory_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) VALUES ('t1', 's1', 'accepted', '2026-01-01T00:00:00Z')"
        )
        # Verify version and input_message_id defaults
        row = in_memory_db.execute(
            "SELECT input_message_id, version FROM turns WHERE turn_id='t1'"
        ).fetchone()
        assert row["input_message_id"] == ""
        assert row["version"] == 1


class TestConfigIntegrity:
    """End-to-end tests for cogito init and config."""

    def test_init_creates_database(self):
        """Verify that cogito init creates a working database."""
        import os
        import tempfile
        from cogito.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            config = Config(workspace_path=tmp)
            db_path = config.resolve_db_path()
            from cogito.store.connection import get_connection
            from cogito.store.migration import migrate
            conn = get_connection(db_path)
            try:
                migrate(conn)
                # Verify turns table exists
                tables = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                assert "turns" in tables
                # Verify turn is v2 schema
                cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
                assert "input_message_id" in cols
                assert "version" in cols
            finally:
                conn.close()
