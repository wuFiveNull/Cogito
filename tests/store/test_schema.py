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
            "deliveries", "delivery_attempts", "delivery_receipts",
            "tool_calls", "approvals",
            "memory_items",
            "memory_relations", "memory_sources",
            "session_summaries", "processing_watermarks",
            "model_calls",
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
        from pathlib import Path
        from cogito.store.migration import (
            migrate, MIGRATIONS_DIR, PLUGIN_MIGRATIONS_DIR,
        )
        migrate(empty_db)
        migrate(empty_db)  # second run
        rows = empty_db.execute("SELECT version FROM _schema_version ORDER BY version").fetchall()
        versions = [r[0] for r in rows]
        # 动态计算预期版本列表（core + plugin 迁移），避免每加一个 migration 就改一次断言
        core_files = [
            p for p in Path(MIGRATIONS_DIR).glob("*.sql")
            if p.name[:4].isdigit()
        ]
        plugin_files = [
            p for p in Path(PLUGIN_MIGRATIONS_DIR).glob("*.sql")
            if p.name[:4].isdigit()
        ] if PLUGIN_MIGRATIONS_DIR.exists() else []
        expected = sorted(int(p.name[:4]) for p in core_files + plugin_files)
        assert versions == expected  # all migration versions applied exactly once

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
        from pathlib import Path
        from cogito.store.migration import MIGRATIONS_DIR, PLUGIN_MIGRATIONS_DIR
        core = {int(p.name[:4]) for p in Path(MIGRATIONS_DIR).glob("*.sql")
                if p.name[:4].isdigit()}
        plugin = {int(p.name[:4]) for p in Path(PLUGIN_MIGRATIONS_DIR).glob("*.sql")
                  if p.name[:4].isdigit()} if PLUGIN_MIGRATIONS_DIR.exists() else set()
        expected = core | plugin
        assert versions == expected

    def test_fresh_install_versions(self, empty_db):
        """Fresh migration from scratch applies all versions."""
        from cogito.store.migration import migrate

        migrate(empty_db)
        versions = {
            r[0] for r in empty_db.execute(
                "SELECT version FROM _schema_version"
            ).fetchall()
        }
        from pathlib import Path
        from cogito.store.migration import MIGRATIONS_DIR, PLUGIN_MIGRATIONS_DIR
        core = {int(p.name[:4]) for p in Path(MIGRATIONS_DIR).glob("*.sql")
                if p.name[:4].isdigit()}
        plugin = {int(p.name[:4]) for p in Path(PLUGIN_MIGRATIONS_DIR).glob("*.sql")
                  if p.name[:4].isdigit()} if PLUGIN_MIGRATIONS_DIR.exists() else set()
        expected = core | plugin
        assert versions == expected

    def test_turn_schema_v2(self, in_memory_db):
        """Verify v2 schema constraints."""
        # Insert a turn with accepted status (v2 default)
        in_memory_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) VALUES ('t1', 's1', 'accepted', 1736942520000)"
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
            "VALUES ('e1', 'TurnAccepted', 'turn', 't1', 1, 1736942520000)"
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
                "INSERT INTO outbox_events (event_id, status, created_at) VALUES ('e1', 'invalid_status', 1736942520000)"
            )

    def test_outbox_aggregate_unique(self, in_memory_db):
        """events table enforces (aggregate_type, aggregate_id, aggregate_version) unique."""
        in_memory_db.execute(
            "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, aggregate_version, occurred_at) "
            "VALUES ('e1', 'TurnAccepted', 'turn', 't1', 1, 1736942520000)"
        )
        with pytest.raises(Exception):
            in_memory_db.execute(
                "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, aggregate_version, occurred_at) "
                "VALUES ('e2', 'TurnAccepted', 'turn', 't1', 1, 1736942520000)"
            )

    def test_outbox_leased_transition(self, in_memory_db):
        in_memory_db.execute(
            "INSERT INTO outbox_events (event_id, status, created_at) VALUES ('e1', 'pending', 1736942520000)"
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


# =============================================================================
# v5/v6 升级测试（PR 8.3-D/8.3-E）
# =============================================================================


class TestVersionUpgrade:
    """测试从 v5/v6 升级到最新版本的兼容性。"""

    def test_v5_to_latest(self, empty_db):
        """构造真实 v5 数据库并升级到最新。"""
        from cogito.store.migration import (
            MIGRATIONS_DIR,
            _ensure_schema_version_table,
            migrate,
        )

        # 手动依序应用 v1-v4
        versions = [1, 2, 3, 4]
        for v in versions:
            files = sorted(MIGRATIONS_DIR.glob(f"{v:04d}_*.sql"))
            if files:
                _ensure_schema_version_table(empty_db)
                empty_db.executescript(files[0].read_text(encoding="utf-8"))
                empty_db.execute(
                    "INSERT INTO _schema_version (version, checksum) VALUES (?, '')", (v,)
                )
                empty_db.commit()

        # 此时是 v4 架构（所有时间列 TEXT），插入 v5 前兼容的数据
        import uuid
        sid = uuid.uuid4().hex
        empty_db.execute(
            "INSERT INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
            "VALUES ('c1', 'private', 'c1')"
        )
        empty_db.execute(
            "INSERT INTO sessions (session_id, conversation_id, context_partition_key, created_at) "
            "VALUES (?, 'c1', 'c1', '2026-01-01T00:00:00Z')", (sid,)
        )
        empty_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) "
            "VALUES ('t1', ?, 'queued', '2026-01-01T00:00:00Z')", (sid,)
        )
        empty_db.execute(
            "INSERT INTO outbox_events (event_id, event_type, aggregate_id, aggregate_version, created_at) "
            "VALUES ('e1', 'TestEvent', 't1', 1, '2026-01-01T00:00:00Z')"
        )
        empty_db.execute(
            "INSERT INTO deliveries (delivery_id, target_snapshot, status, idempotency_key, created_at) "
            "VALUES ('d1', '{}', 'pending', 'k1', '2026-01-01T00:00:00Z')"
        )
        empty_db.commit()

        # 升级到最新
        migrate(empty_db)

        # 验证数据存活且时间正确转换
        turn = empty_db.execute(
            "SELECT status, created_at FROM turns WHERE turn_id='t1'"
        ).fetchone()
        assert turn["status"] == "queued"
        assert isinstance(turn["created_at"], int), "created_at should be INTEGER after migration"

        # 验证 delivery_receipts 表存在（migration 0008）
        tables = {
            r[0] for r in empty_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "delivery_receipts" in tables

    def test_v6_to_latest_with_reliability_fields(self, empty_db):
        """构造 v6 数据库（已有可靠性字段，时间仍为 TEXT）并升级。"""
        from cogito.store.migration import (
            MIGRATIONS_DIR,
            _ensure_schema_version_table,
            migrate,
        )

        # 应用 v1-v6
        for v in range(1, 7):
            files = sorted(MIGRATIONS_DIR.glob(f"{v:04d}_*.sql"))
            if files:
                _ensure_schema_version_table(empty_db)
                empty_db.executescript(files[0].read_text(encoding="utf-8"))
                empty_db.execute(
                    "INSERT INTO _schema_version (version, checksum) VALUES (?, '')", (v,)
                )
                empty_db.commit()

        # 插入 v6 风格数据（TEXT 时间 + 可靠性字段）
        empty_db.execute(
            "INSERT INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
            "VALUES ('c1', 'private', 'c1')"
        )
        empty_db.execute(
            "INSERT INTO sessions (session_id, conversation_id, context_partition_key, created_at) "
            "VALUES ('s1', 'c1', 'c1', '2026-01-01T00:00:00Z')"
        )
        empty_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) "
            "VALUES ('t1', 's1', 'queued', '2026-01-01T00:00:00Z')"
        )
        # v6 增加 reliability 字段（still TEXT）
        empty_db.execute(
            "INSERT INTO deliveries (delivery_id, target_snapshot, status, idempotency_key, "
            "attempt_count, lease_owner, lease_version, created_at) "
            "VALUES ('d1', '{}', 'pending', 'k1', 1, 'w1', 1, '2026-01-01T00:00:00Z')"
        )
        empty_db.commit()

        # 升级到最新
        migrate(empty_db)

        # 验证数据完整
        turn = empty_db.execute(
            "SELECT status FROM turns WHERE turn_id='t1'"
        ).fetchone()
        assert turn is not None
        assert turn["status"] == "queued"

        delivery = empty_db.execute(
            "SELECT status, attempt_count FROM deliveries WHERE delivery_id='d1'"
        ).fetchone()
        assert delivery is not None
        assert delivery["status"] == "pending"
        assert delivery["attempt_count"] == 1

    def test_iso_epoch_and_null_migration(self, empty_db):
        """ISO、epoch ms 和 NULL 分别验证可正确升级。"""
        from cogito.store.migration import (
            MIGRATIONS_DIR,
            _ensure_schema_version_table,
            migrate,
        )

        # 应用 v1-v6
        for v in range(1, 7):
            files = sorted(MIGRATIONS_DIR.glob(f"{v:04d}_*.sql"))
            if files:
                _ensure_schema_version_table(empty_db)
                empty_db.executescript(files[0].read_text(encoding="utf-8"))
                empty_db.execute(
                    "INSERT INTO _schema_version (version, checksum) VALUES (?, '')", (v,)
                )
                empty_db.commit()

        # ISO, epoch ms, NULL 三种时间格式
        empty_db.execute(
            "INSERT INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
            "VALUES ('c1', 'private', 'c1')"
        )
        empty_db.execute(
            "INSERT INTO sessions (session_id, conversation_id, context_partition_key, created_at) "
            "VALUES ('s1', 'c1', 'c1', '2026-01-01T00:00:00Z')"
        )
        # ISO
        empty_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) "
            "VALUES ('t_iso', 's1', 'queued', '2026-01-15T12:00:00Z')"
        )
        # epoch ms as text
        empty_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) "
            "VALUES ('t_epoch', 's1', 'queued', '1736942520000')"
        )
        # NULL on nullable column (lease_expires_at)
        empty_db.execute(
            "INSERT INTO turns (turn_id, session_id, status, created_at) "
            "VALUES ('t_null', 's1', 'queued', '2026-01-01T00:00:00Z')"
        )
        empty_db.commit()

        migrate(empty_db)

        # ISO → 正确转换
        t_iso = empty_db.execute(
            "SELECT created_at FROM turns WHERE turn_id='t_iso'"
        ).fetchone()
        assert isinstance(t_iso["created_at"], int)

        # epoch ms as text → 正确转换
        t_epoch = empty_db.execute(
            "SELECT created_at FROM turns WHERE turn_id='t_epoch'"
        ).fetchone()
        assert isinstance(t_epoch["created_at"], int)
        assert t_epoch["created_at"] == 1736942520000

        # NULL → 保持 NULL（在可空列上验证）
        t_null = empty_db.execute(
            "SELECT cancel_requested_at FROM turns WHERE turn_id='t_null'"
        ).fetchone()
        assert t_null["cancel_requested_at"] is None

    def test_migration_foreign_key_check(self, in_memory_db):
        """migrate 后 foreign_key_check 返回空结果。"""
        violations = in_memory_db.execute("PRAGMA foreign_key_check").fetchall()
        assert len(violations) == 0
