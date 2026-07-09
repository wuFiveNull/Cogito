"""Track E: Config + Restore + Runbook — Plan 06 M2/M8/M10."""
from __future__ import annotations

from cogito.infrastructure.backup import BackupService
from cogito.infrastructure.config_version import (
    hot_reload_dry_run,
    normalize_config,
    secret_ref,
    validate_cross_fields,
)
from cogito.infrastructure.restore import RestoreService
from cogito.infrastructure.runbook import get_runbook, release_gates


def test_normalize_config_computes_hash() -> None:
    cfg = normalize_config({"agent": {"model": "claude"}})
    assert "content_hash" in cfg
    assert len(cfg["content_hash"]) == 16


def test_secret_only_stores_ref() -> None:
    """Secret 只保存引用，不存明文。"""
    ref = secret_ref("OPENAI_API_KEY")
    assert "env://" in ref
    assert "sk-" not in ref


def test_hot_reload_detects_unknown_keys() -> None:
    """热更新检测未知字段。"""
    errors = hot_reload_dry_run({"unknown_key": 1})
    assert any("unknown" in e for e in errors)


def test_hot_reload_passes_valid_config() -> None:
    errors = hot_reload_dry_run({"agent": {"model": "claude"}})
    assert errors == []


def test_cross_field_validation() -> None:
    """跨字段校验（Plan 06 M2）。"""
    errors = validate_cross_fields({"worker": {"heartbeat_s": 10, "lease_ttl_s": 15},
                                    "agent": {"max_output_tokens": 4096}})
    # heartbeat*2=20 < lease_ttl=15 fails
    assert len(errors) >= 1


def test_restore_verify_before_restore() -> None:
    """恢复前先 verify。"""
    import sqlite3, tempfile
    tmp = tempfile.mkdtemp()
    db_conn = sqlite3.connect(":memory:")
    svc = BackupService(tmp, db_conn)
    restore = RestoreService(svc)
    result = restore.restore("nonexistent")
    assert result.status == "failed"


def test_restore_needs_confirmation() -> None:
    """恢复后需人工确认。"""
    import sqlite3, tempfile
    tmp = tempfile.mkdtemp()
    db_conn = sqlite3.connect(":memory:")
    db_conn.execute("CREATE TABLE t (id TEXT)")
    db_conn.commit()
    svc = BackupService(tmp, db_conn)
    m = svc.create()
    restore = RestoreService(svc)
    result = restore.restore(m.backup_id)
    assert result.status == "needs_confirmation"


def test_runbook_covers_10_scenarios() -> None:
    """Runbook 覆盖 10+ 故障场景 (Plan 06 M10)。"""
    scenarios = get_runbook()
    assert len(scenarios) >= 10


def test_release_gates_10_items() -> None:
    """发布门禁 10 项 (Plan 06 M10)。"""
    gates = release_gates()
    assert len(gates) == 10
    assert all(gates.values())
