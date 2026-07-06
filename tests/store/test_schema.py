"""Tests for SQLite schema and migration."""

import pytest


class TestMigration:
    def test_migrate_creates_tables(self, in_memory_db):
        """After migration, all core tables should exist."""
        tables = [
            "principals", "endpoints", "conversations", "sessions",
            "messages", "content_parts", "message_revisions", "inbound_inbox",
            "turns", "run_attempts", "turn_checkpoints",
            "tasks", "task_attempts",
            "deliveries", "delivery_attempts",
            "tool_calls", "approvals",
            "memory_items",
            "events", "outbox_events",
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
        assert versions == [1, 2, 3, 4, 5]  # all migration versions applied exactly once

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
        from cogito.store.migration import MIGRATIONS_DIR, _ensure_schema_version_table, migrate

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
        from cogito.store.migration import MIGRATIONS_DIR, _ensure_schema_version_table, migrate

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
        assert versions == {1, 2, 3, 4, 5}

    def test_fresh_install_versions(self, empty_db):
        """Fresh migration from scratch applies all versions."""
        from cogito.store.migration import migrate

        migrate(empty_db)
        versions = {
            r[0] for r in empty_db.execute(
                "SELECT version FROM _schema_version"
            ).fetchall()
        }
        assert versions == {1, 2, 3, 4, 5}

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


class TestOutboxSchema:
    """Tests for outbox_events and events tables (migration 0003)."""

    def test_outbox_event_insert_and_query(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO outbox_events (event_id, event_type, aggregate_type, aggregate_id, aggregate_version, created_at) "
            "VALUES ('e1', 'TurnAccepted', 'turn', 't1', 1, '2026-01-01T00:00:00Z')"
        )
        row = in_memory_db.execute(
            "SELECT status, event_type FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["status"] == "pending"
        assert row["event_type"] == "TurnAccepted"

    def test_outbox_status_constraint(self, in_memory_db):
        """Invalid status should be rejected."""
        with pytest.raises(Exception):
            in_memory_db.execute(
                "INSERT INTO outbox_events (event_id, status, created_at) VALUES ('e1', 'invalid_status', '2026-01-01T00:00:00Z')"
            )

    def test_outbox_aggregate_unique(self, in_memory_db):
        """events table enforces (aggregate_type, aggregate_id, aggregate_version) unique."""
        in_memory_db.execute(
            "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, aggregate_version, occurred_at) "
            "VALUES ('e1', 'TurnAccepted', 'turn', 't1', 1, '2026-01-01T00:00:00Z')"
        )
        with pytest.raises(Exception):
            in_memory_db.execute(
                "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, aggregate_version, occurred_at) "
                "VALUES ('e2', 'TurnAccepted', 'turn', 't1', 1, '2026-01-01T00:00:00Z')"
            )

    def test_outbox_leased_transition(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO outbox_events (event_id, status, created_at) VALUES ('e1', 'pending', '2026-01-01T00:00:00Z')"
        )
        in_memory_db.execute(
            "UPDATE outbox_events SET status='leased', lease_owner='worker1' WHERE event_id='e1'"
        )
        row = in_memory_db.execute(
            "SELECT status, lease_owner FROM outbox_events WHERE event_id='e1'"
        ).fetchone()
        assert row["status"] == "leased"
        assert row["lease_owner"] == "worker1"


class TestMessageRevisionSchema:
    """Tests for message_revisions table (migration 0003)."""

    def test_insert_revision(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
            "VALUES ('c1', 'private', 'plat_c1')"
        )
        in_memory_db.execute(
            "INSERT INTO messages (message_id, conversation_id, role, created_at) "
            "VALUES ('m1', 'c1', 'user', '2026-01-01T00:00:00Z')"
        )
        in_memory_db.execute(
            "INSERT INTO message_revisions (message_id, revision_no, platform_edit_id, created_at) "
            "VALUES ('m1', 1, 'pe1', '2026-01-01T00:00:00Z')"
        )
        row = in_memory_db.execute(
            "SELECT revision_no, platform_edit_id FROM message_revisions WHERE message_id='m1'"
        ).fetchone()
        assert row["revision_no"] == 1
        assert row["platform_edit_id"] == "pe1"

    def test_revision_unique_per_message(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
            "VALUES ('c1', 'private', 'plat_c1')"
        )
        in_memory_db.execute(
            "INSERT INTO messages (message_id, conversation_id, role, created_at) "
            "VALUES ('m1', 'c1', 'user', '2026-01-01T00:00:00Z')"
        )
        in_memory_db.execute(
            "INSERT INTO message_revisions (message_id, revision_no, platform_edit_id, created_at) "
            "VALUES ('m1', 1, 'pe1', '2026-01-01T00:00:00Z')"
        )
        with pytest.raises(Exception):
            in_memory_db.execute(
                "INSERT INTO message_revisions (message_id, revision_no, platform_edit_id, created_at) "
                "VALUES ('m1', 1, 'pe2', '2026-01-01T00:00:00Z')"
            )

    def test_revision_fk_enforced(self, in_memory_db):
        """FK to messages should be enforced."""
        with pytest.raises(Exception):
            in_memory_db.execute(
                "INSERT INTO message_revisions (message_id, revision_no, created_at) "
                "VALUES ('nonexistent', 1, '2026-01-01T00:00:00Z')"
            )


class TestMessageDeletedAt:
    """Tests for messages.deleted_at column (migration 0003)."""

    def test_deleted_at_default_null(self, in_memory_db):
        row = in_memory_db.execute(
            "PRAGMA table_info(messages)"
        ).fetchall()
        cols = {r[1] for r in row}
        assert "deleted_at" in cols

    def test_soft_delete_and_query(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
            "VALUES ('c1', 'private', 'plat_c1')"
        )
        in_memory_db.execute(
            "INSERT INTO messages (message_id, conversation_id, role, created_at) "
            "VALUES ('m1', 'c1', 'user', '2026-01-01T00:00:00Z')"
        )
        in_memory_db.execute(
            "UPDATE messages SET deleted_at = '2026-06-01T00:00:00Z' WHERE message_id='m1'"
        )
        row = in_memory_db.execute(
            "SELECT deleted_at FROM messages WHERE message_id='m1'"
        ).fetchone()
        assert row["deleted_at"] is not None
