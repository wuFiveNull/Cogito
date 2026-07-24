"""Tests for Plan 06 M5 / T4 — Schema completion (new tables + partial indexes)."""

import json

import pytest

from cogito.store.capability_repo import CapabilityRecord, CapabilityRepository
from cogito.store.command_audit_repo import CommandAuditRepository, CommandRecord
from cogito.store.config_version_repo import ConfigVersionRecord, ConfigVersionRepository
from cogito.store.context_snapshot_repo import (
    ContextSnapshotRecord,
    ContextSnapshotRepository,
    SnapshotItem,
)
from cogito.store.receipt_repo import ReceiptRecord, SideEffectReceiptRepository


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def conn(in_memory_db):
    return in_memory_db


# ── 6.1: 新表存在性 ───────────────────────────────────────


class TestNewTablesExist:
    """sch-01: 空库应用 migration 0029-0036 后，所有新表存在。"""

    EXPECTED_NEW_TABLES = [
        "commands",
        "capabilities",
        "context_snapshots",
        "context_snapshot_items",
        "config_versions",
        "gateway_operation_receipts",
        "plugins",
        "plugin_snapshots",
        "plugin_runtime_audit",
    ]

    def test_new_tables_exist(self, conn):
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for tbl in self.EXPECTED_NEW_TABLES:
            assert tbl in existing, f"Missing table: {tbl}"

    def test_partial_indexes_exist(self, conn):
        """sch-09: 部分索引在 sqlite_master 中可见。"""
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            ).fetchall()
        }
        expected = {
            "idx_turns_active_session",
            "idx_tasks_queued",
            "idx_outbox_pending",
            "idx_deliveries_inflight",
            "idx_approvals_pending",
            "idx_schedules_due",
        }
        for idx in expected:
            assert idx in indexes, f"Missing partial index: {idx}"


# ── 6.2: commands 表 ──────────────────────────────────────


class TestCommandsTable:
    """sch-04: commands 唯一约束（actor, type, idempotency）冲突。"""

    def test_insert_and_find(self, conn):
        repo = CommandAuditRepository(conn)
        rec = CommandRecord(
            command_id="cmd-1",
            actor="owner",
            command_type="CancelTurn",
            idempotency_key="idem-1",
            target_type="turn",
            target_id="turn-1",
            expected_version=2,
            payload='{"reason":"user"}',
            created_at=1000,
        )
        repo.insert(rec)
        conn.commit()

        found = repo.find_by_idempotency("owner", "CancelTurn", "idem-1")
        assert found is not None
        assert found.command_id == "cmd-1"
        assert found.status == "pending"

    def test_idempotency_unique_constraint(self, conn):
        repo = CommandAuditRepository(conn)
        rec = CommandRecord(
            command_id="cmd-a",
            actor="owner",
            command_type="RetryTurn",
            idempotency_key="idem-x",
            target_type=None,
            target_id=None,
            expected_version=None,
            payload=None,
            created_at=1000,
        )
        repo.insert(rec)
        conn.commit()

        dup = CommandRecord(
            command_id="cmd-b",
            actor="owner",
            command_type="RetryTurn",
            idempotency_key="idem-x",
            target_type=None,
            target_id=None,
            expected_version=None,
            payload=None,
            created_at=2000,
        )
        with pytest.raises(Exception):  # IntegrityError
            repo.insert(dup)
            conn.commit()

    def test_mark_consumed(self, conn):
        repo = CommandAuditRepository(conn)
        rec = CommandRecord(
            command_id="cmd-c",
            actor="owner",
            command_type="Approve",
            idempotency_key="idem-c",
            target_type=None,
            target_id=None,
            expected_version=None,
            payload=None,
            created_at=1000,
        )
        repo.insert(rec)
        conn.commit()
        repo.mark_consumed("cmd-c", result_summary="approved")
        conn.commit()
        assert repo.get("cmd-c").status == "consumed"


# ── 6.3: side-effect receipt Event 回放 ────────────────────


class TestSideEffectReceipts:
    def test_insert_and_find_by_attempt(self, conn):
        repo = SideEffectReceiptRepository(conn)
        rec = ReceiptRecord(
            receipt_id="r-1",
            capability_id="ns:tool",
            operation_id="op-123",
            request_hash="sha256:abc",
            side_effect_class="idempotent",
            status="succeeded",
            attempt_id="att-1",
            attempt_type="run",
            created_at=1000,
        )
        repo.insert(rec)
        conn.commit()

        found = repo.find_by_attempt("run", "att-1")
        assert len(found) == 1
        assert found[0].operation_id == "op-123"
        assert conn.execute("SELECT COUNT(*) FROM side_effect_receipts").fetchone()[0] == 0

    def test_pending_reconcile(self, conn):
        repo = SideEffectReceiptRepository(conn)
        rec = ReceiptRecord(
            receipt_id="r-2",
            capability_id="ns:tool2",
            operation_id=None,
            request_hash="sha256:def",
            side_effect_class="reconcilable",
            status="unknown",
            reconcile_status="pending",
            attempt_id="att-2",
            attempt_type="task",
            created_at=1000,
        )
        repo.insert(rec)
        conn.commit()

        pending = repo.find_pending_reconcile()
        assert len(pending) == 1
        assert pending[0].reconcile_status == "pending"


# ── 6.4: capabilities 表 ──────────────────────────────────


class TestCapabilities:
    def test_upsert_and_list_healthy(self, conn):
        repo = CapabilityRepository(conn)
        rec = CapabilityRecord(
            capability_id="ns:tool",
            kind="tool",
            version="1.0",
            owner="core",
            toolsets=["default"],
            supported_modes=["terminal", "proactive"],
            permissions=["read"],
            risk_level="low",
            side_effect_class="idempotent",
            health="healthy",
            discovered_at=1000,
            updated_at=1000,
        )
        repo.upsert(rec)
        conn.commit()

        healthy = repo.list_healthy()
        assert len(healthy) == 1
        assert healthy[0].toolsets == ["default"]

    def test_update_health(self, conn):
        repo = CapabilityRepository(conn)
        rec = CapabilityRecord(
            capability_id="ns:t2",
            kind="tool",
            version="1.0",
            health="healthy",
            discovered_at=1000,
            updated_at=1000,
        )
        repo.insert(rec)
        conn.commit()
        repo.update_health("ns:t2", "degraded")
        conn.commit()
        assert repo.get("ns:t2").health == "degraded"


# ── 6.5: context_snapshots 表 ─────────────────────────────


class TestContextSnapshots:
    """sch-07: context_snapshot_items 外键约束。"""

    def test_insert_with_items(self, conn):
        repo = ContextSnapshotRepository(conn)
        rec = ContextSnapshotRecord(
            snapshot_id="snap-1",
            session_id="sess-1",
            attempt_id="att-1",
            attempt_type="run",
            token_budget=8000,
            tokens_used=4000,
            created_at=1000,
            items=[
                SnapshotItem(
                    item_index=0, source="memory", content_ref="mem:1", score=0.9, tokens=500
                ),
                SnapshotItem(item_index=1, source="recent", content_ref="msg:2", tokens=300),
            ],
        )
        repo.insert(rec)
        conn.commit()

        found = repo.get("snap-1")
        assert found is not None
        assert len(found.items) == 2
        assert found.items[0].score == 0.9

    def test_foreign_key_constraint(self, conn):
        """直接插入孤儿 item 应失败。"""
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO context_snapshot_items "
                "(snapshot_id, item_index, source, content_ref) "
                "VALUES (?, ?, ?, ?)",
                ("nonexistent", 0, "x", "y"),
            )
            conn.commit()


# ── 6.7: config_versions 表 ───────────────────────────────


class TestConfigVersions:
    def test_insert_and_find_by_hash(self, conn):
        repo = ConfigVersionRepository(conn)
        rec = ConfigVersionRecord(
            version_id="cfg-1",
            content_hash="a1b2c3",
            schema_version="1",
            source_layers=["profile"],
            applied_at=1000,
            applied_by="owner",
        )
        repo.insert(rec)
        conn.commit()

        found = repo.get_by_hash("a1b2c3")
        assert found is not None
        assert found.schema_version == "1"

    def test_latest(self, conn):
        repo = ConfigVersionRepository(conn)
        repo.insert(
            ConfigVersionRecord(
                version_id="cfg-1",
                content_hash="h1",
                schema_version="1",
                source_layers=[],
                applied_at=1000,
            )
        )
        repo.insert(
            ConfigVersionRecord(
                version_id="cfg-2",
                content_hash="h2",
                schema_version="1",
                source_layers=[],
                applied_at=2000,
            )
        )
        conn.commit()
        assert repo.latest().version_id == "cfg-2"


# ── 6.8: 升级路径 ─────────────────────────────────────────


class TestUpgradePath:
    """sch-02: 从上一版本（0028）升级到最新成功。
    注：in_memory_db fixture 已运行完整 migrate()，此处验证版本号。
    """

    def test_current_version_at_least_29(self, conn):
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        assert row[0] >= 29, f"Expected version >= 29, got {row[0]}"

    def test_idempotent_reapply(self, conn):
        """sch-03: 重复启动幂等 —— 再次运行 migrate() 不报错。"""
        from cogito.store.migration import migrate

        migrate(conn)  # 不应抛异常
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        assert row[0] >= 29
