"""PR-O7+O8+O9: Backup + Startup/Shutdown — Plan 06 M7/M9."""

from __future__ import annotations

import sqlite3
import tempfile

from cogito.infrastructure.backup import (
    BackupService,
    shutdown_sequence,
    startup_sequence,
)


class _DB:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
        self.conn.execute("INSERT INTO t VALUES ('x')")
        self.conn.commit()


def test_backup_creates_snapshot() -> None:
    """备份创建 SQLite snapshot。"""
    tmp = tempfile.mkdtemp()
    db = _DB()
    svc = BackupService(tmp, db.conn)
    manifest = svc.create()
    assert manifest.backup_id != ""
    assert manifest.sqlite_snapshot_uri.endswith(".db")


def test_backup_verify() -> None:
    """备份验证完整性。"""
    tmp = tempfile.mkdtemp()
    db = _DB()
    svc = BackupService(tmp, db.conn)
    m = svc.create()
    assert svc.verify(m.backup_id) is True
    assert svc.verify("nonexistent") is False


def test_startup_sequence_strict() -> None:
    """启动顺序严格执行 (Plan 06 M9)。"""
    seq = startup_sequence()
    assert len(seq) == 11
    assert "Recovery" in seq[6]
    assert "Gateway" in seq[9]
    assert "Web" in seq[10]


def test_shutdown_sequence() -> None:
    """关闭 drain (Plan 06 M9)。"""
    seq = shutdown_sequence()
    assert len(seq) == 6
    assert "Checkpoint" in seq[2]
    assert "unknown" in seq[3]
    assert "不强停" in seq[5] or "completed" in seq[5]


def test_recovery_reads_before_ready() -> None:
    """Recovery 完成前 readiness=false (Plan 06 M9)。"""
    seq = startup_sequence()
    recovery_idx = next(i for i, s in enumerate(seq) if "Recovery" in s)
    api_idx = next(i for i, s in enumerate(seq) if "API" in s)
    assert recovery_idx < api_idx
