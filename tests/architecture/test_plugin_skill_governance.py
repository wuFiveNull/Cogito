"""PR-C7+C8: Plugin Runtime + Skill lifecycle + Governance — Plan 03 M7/M8."""
from __future__ import annotations

from cogito.capability.plugin_runtime import (
    CircuitBreaker,
    PluginManifest,
    SqlitePluginRuntime,
)


# ── Plugin lifecycle ────────────────────────────────────────────

def test_plugin_install_enable() -> None:
    rt = SqlitePluginRuntime(conn=None)
    m = PluginManifest(plugin_id="p1", version="1.0", api_version="1")
    rt.install(m)
    s = rt.enable("p1")
    assert s is not None
    assert s.status == "enabled"


def test_plugin_api_version_incompatible() -> None:
    rt = SqlitePluginRuntime(conn=None)
    m = PluginManifest(plugin_id="old", api_version="0")
    assert rt.validate(m) is False


def test_plugin_subprocess_default() -> None:
    m = PluginManifest(plugin_id="ext")
    assert m.subprocess is True  # 第三方默认进程外


def test_plugin_disable() -> None:
    rt = SqlitePluginRuntime(conn=None)
    m = PluginManifest(plugin_id="p1", api_version="1")
    rt.install(m)
    rt.enable("p1")
    s = rt.disable("p1")
    assert s is not None
    assert s.status == "disabled"


# ── Circuit breaker (Plan 03 M8) ────────────────────────────────

def test_circuit_breaker_triggers_after_3() -> None:
    cb = CircuitBreaker(max_failures=3, window_s=60)
    assert cb.is_ok is True
    cb.record_failure()
    cb.record_failure()
    assert cb.is_ok is True
    cb.record_failure()
    assert cb.should_disable is True
    assert cb.is_ok is False


def test_circuit_breaker_recovers_after_success() -> None:
    cb = CircuitBreaker(max_failures=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.is_ok is True


def test_plugin_degraded_on_circuit_break() -> None:
    """连续失败触发熔断 → 插件进入 degraded。"""
    rt = SqlitePluginRuntime(conn=None)
    m = PluginManifest(plugin_id="flaky", api_version="1")
    rt.install(m)
    rt.enable("flaky")
    for _ in range(3):
        rt.record_failure("flaky")
    s = rt.get("flaky")
    assert s is not None
    assert s.status == "degraded"


# ── Governance commands (enable/disable/inspect/reconcile) ───────

def test_governance_enable_disable_commands() -> None:
    """控制命令 enable/disable plugin 完整。"""
    rt = SqlitePluginRuntime(conn=None)
    m = PluginManifest(plugin_id="g1", api_version="1")
    rt.install(m)
    assert rt.enable("g1").status == "enabled"
    assert rt.disable("g1").status == "disabled"


def test_governance_list_active() -> None:
    rt = SqlitePluginRuntime(conn=None)
    rt.install(PluginManifest(plugin_id="a", api_version="1"))
    rt.install(PluginManifest(plugin_id="b", api_version="1"))
    rt.enable("a")
    rt.enable("b")
    assert len(rt.list_all()) == 2
