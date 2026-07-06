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
        count = empty_db.execute("SELECT COUNT(*) FROM _schema_version").fetchone()[0]
        assert count == 1  # only one version record

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
