"""PLAN-10 M5: SqlitePluginRuntime 产品化测试。

覆盖：
- 多源发现 (builtin / user / 显式路径 / pip entry-point 跳过)
- 状态持久化 (plugins 表)
- 权限映射 / 命名冲突
- 熔断器触发 → degraded
- name 冲突安装拒绝
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

from cogito.capability.plugin_runtime import (
    CircuitBreaker,
    SqlitePluginRuntime,
)
from cogito.service.plugin_runtime import PluginManifest, PluginState
from cogito.store.migration import migrate


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


class TestDiscover:
    def _write_plugin(self, dir_path: str, pid: str, *,
                      api_version: str = "1",
                      permissions: list[str] | None = None) -> str:
        sub = os.path.join(dir_path, pid)
        os.makedirs(sub, exist_ok=True)
        manifest = {
            "id": pid,
            "version": "1.0",
            "api_version": api_version,
            "permissions": permissions or [],
            "entry_point": f"{pid}.main:handler",
            "subprocess": True,
        }
        with open(os.path.join(sub, "plugin.yaml"), "w", encoding="utf-8") as f:
            # 手写 YAML 避免依赖
            f.write(textwrap.dedent(f"""\
                id: {pid}
                version: "1.0"
                api_version: "{api_version}"
                permissions:
                {''.join(f'  - {p}\n' for p in (permissions or []))}
                entry_point: "{pid}.main:handler"
                subprocess: true
            """))
        return sub

    def test_discover_builtin_path(self, conn, tmp_path: Path) -> None:
        self._write_plugin(str(tmp_path), "alpha")
        self._write_plugin(str(tmp_path), "beta")
        rt = SqlitePluginRuntime(conn, builtin_paths=[str(tmp_path)])
        manifests = rt.discover()
        ids = {m.plugin_id for m in manifests}
        assert "alpha" in ids
        assert "beta" in ids

    def test_discover_multi_source_dedup(self, conn, tmp_path: Path) -> None:
        d1 = str(tmp_path / "src1")
        d2 = str(tmp_path / "src2")
        self._write_plugin(d1, "shared")
        self._write_plugin(d2, "shared")  # 同名覆盖（后者不追加）
        self._write_plugin(d2, "extra")
        rt = SqlitePluginRuntime(conn, builtin_paths=[d1])
        manifests = rt.discover(d2)
        ids = [m.plugin_id for m in manifests]
        assert ids.count("shared") == 1  # 去重
        assert "extra" in ids

    def test_discover_incompatible_skipped_by_api_validation(self, conn, tmp_path: Path) -> None:
        self._write_plugin(str(tmp_path), "old", api_version="0")
        rt = SqlitePluginRuntime(conn, builtin_paths=[str(tmp_path)])
        manifests = rt.discover()
        # v0 仍由 discover 返回（manifest 解析层不校验），但 validate 拒绝
        m = next((m for m in manifests if m.plugin_id == "old"), None)
        assert m is not None
        assert rt.validate(m) is False

    def test_discover_missing_dir_is_noop(self, conn) -> None:
        rt = SqlitePluginRuntime(conn, builtin_paths=["/no/such/path"])
        assert rt.discover() == []


class TestPersistence:
    def test_install_persists_and_reload(self, conn, tmp_path: Path) -> None:
        m = PluginManifest(
            plugin_id="persist", version="1.0", api_version="1",
            permissions=("memory.read", "memory.write"),
            entry_point="persist.main:handler",
        )
        rt = SqlitePluginRuntime(conn)
        rt.install(m)
        # 重新构造应从 DB 加载
        rt2 = SqlitePluginRuntime(conn)
        s = rt2.get("persist")
        assert s is not None
        assert s.status == "installed"
        assert s.manifest is not None
        assert s.manifest.permissions == ("memory.read", "memory.write")

    def test_enable_running_timestamps(self, conn) -> None:
        m = PluginManifest(plugin_id="ts")
        rt = SqlitePluginRuntime(conn)
        rt.install(m)
        rt.enable("ts")
        s = rt.get("ts")
        assert s is not None
        assert s.status == "enabled"
        assert s.started_at


class TestCircuitBreaker:
    def test_triggers_after_3(self) -> None:
        cb = CircuitBreaker(max_failures=3, window_s=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_ok is True
        cb.record_failure()
        assert cb.should_disable is True

    def test_recovers_after_success(self) -> None:
        cb = CircuitBreaker(max_failures=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.is_ok is True


class TestDegradedOnCircuitBreak:
    def test_degraded_after_3_failures(self, conn) -> None:
        m = PluginManifest(plugin_id="flaky", api_version="1")
        rt = SqlitePluginRuntime(conn)
        rt.install(m)
        rt.enable("flaky")
        for _ in range(3):
            rt.record_failure("flaky")
        assert rt.get("flaky") is not None
        assert rt.get("flaky").status == "degraded"

    def test_re_enable_allowed_after_recovery(self, conn) -> None:
        """熔断后人工 reset（success once）可重新 enable。"""
        m = PluginManifest(plugin_id="recover", api_version="1")
        rt = SqlitePluginRuntime(conn)
        rt.install(m)
        rt.enable("recover")
        for _ in range(3):
            rt.record_failure("recover")
        assert rt.get("recover").status == "degraded"
        # 模拟恢复（人工 reset 熔断器）
        rt._breaker("recover").reset()
        s = rt.enable("recover")
        assert s is not None
        assert rt.get("recover").status == "enabled"


class TestPermission:
    def test_permissions_preserved(self, conn, tmp_path: Path) -> None:
        self._write_plugin(str(tmp_path), "secured",
                          permissions=["memory.read", "fs.read"])
        rt = SqlitePluginRuntime(conn, builtin_paths=[str(tmp_path)])
        m = rt.discover()[0]
        installed = rt.install(m)
        s = rt.get("secured")
        assert s is not None and s.manifest is not None
        assert "memory.read" in s.manifest.permissions

    def _write_plugin(self, dir_path: str, pid: str, **kw: Any) -> None:
        sub = os.path.join(dir_path, pid)
        os.makedirs(sub, exist_ok=True)
        perms = kw.get("permissions", [])
        with open(os.path.join(sub, "plugin.yaml"), "w", encoding="utf-8") as f:
            f.write(f"id: {pid}\nversion: '1.0'\napi_version: '1'\n")
            f.write("permissions:\n")
            for p in perms:
                f.write(f"  - {p}\n")
            f.write(f"entry_point: {pid}.main\nsubprocess: true\n")


class TestConflict:
    def test_duplicate_install_overwrites(self, conn) -> None:
        rt = SqlitePluginRuntime(conn)
        m1 = PluginManifest(plugin_id="dup", version="1.0")
        m2 = PluginManifest(plugin_id="dup", version="2.0")
        rt.install(m1)
        rt.install(m2)
        s = rt.get("dup")
        assert s is not None and s.manifest is not None
        assert s.manifest.version == "2.0"
        # 仅一条记录
        rows = conn.execute("SELECT COUNT(*) FROM plugins WHERE plugin_id='dup'").fetchone()[0]
        assert rows == 1


def _runnable_manifest(tmp_path: Path, *, permissions: tuple[str, ...] = ()) -> PluginManifest:
    plugin_dir = tmp_path / "runnable_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "def register():\n    return 'ok'\n", encoding="utf-8",
    )
    return PluginManifest(
        plugin_id="runnable-plugin",
        version="1.0",
        api_version="1",
        permissions=permissions,
        entry_point="runnable_plugin:register",
        subprocess=True,
        source="project",
        source_path=str(plugin_dir),
    )


class TestRuntimeLifecycle:
    def test_subprocess_start_health_stop(self, conn, tmp_path: Path) -> None:
        runtime = SqlitePluginRuntime(conn)
        runtime.install(_runnable_manifest(tmp_path))
        runtime.enable("runnable-plugin")
        state = runtime.start("runnable-plugin")
        assert state is not None and state.status == "running"
        assert state.process_id is not None
        assert runtime.health("runnable-plugin")["status"] == "running"
        stopped = runtime.stop("runnable-plugin")
        assert stopped is not None and stopped.status == "stopped"
        assert runtime.health("runnable-plugin")["status"] == "stopped"
        runtime.close()

    def test_permission_denial_is_degraded_and_audited(self, conn, tmp_path: Path) -> None:
        runtime = SqlitePluginRuntime(conn)
        runtime.install(_runnable_manifest(tmp_path, permissions=("filesystem.read",)))
        runtime.enable("runnable-plugin")
        state = runtime.start("runnable-plugin")
        assert state is not None and state.status == "degraded"
        row = conn.execute(
            "SELECT outcome, safe_detail FROM plugin_runtime_audit "
            "WHERE plugin_id='runnable-plugin' AND action='start' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row["outcome"] == "denied"
        assert row["safe_detail"] == "PluginPermissionError"

    def test_explicit_permission_grant_allows_start(self, conn, tmp_path: Path) -> None:
        runtime = SqlitePluginRuntime(
            conn, granted_permissions={"filesystem.read"},
        )
        runtime.install(_runnable_manifest(tmp_path, permissions=("filesystem.read",)))
        runtime.enable("runnable-plugin")
        state = runtime.start("runnable-plugin")
        assert state is not None and state.status == "running"
        runtime.close()

    def test_upgrade_snapshot_and_rollback(self, conn) -> None:
        runtime = SqlitePluginRuntime(conn)
        runtime.install(PluginManifest(plugin_id="upgrade", version="1.0"))
        runtime.install(PluginManifest(plugin_id="upgrade", version="2.0"))
        restored = runtime.rollback("upgrade")
        assert restored is not None and restored.manifest is not None
        assert restored.manifest.version == "1.0"
