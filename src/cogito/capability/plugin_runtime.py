"""Plugin Runtime — 多源发现 + Manifest + 生命周期 + 熔断 (Plan 03 M7/M8).

生命周期：discovered → validated → installed → configured → enabled → started → running。
第三方默认 subprocess；连续 3 次/60s 触发熔断；升级前保存快照，失败自动回滚。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class PluginManifest:
    """插件声明（Plan 03 M7）。"""
    plugin_id: str
    version: str = "1.0"
    api_version: str = "1"
    permissions: tuple[str, ...] = ()
    entry_point: str = ""
    config_schema: dict[str, Any] | None = None
    dependencies: tuple[str, ...] = ()
    subprocess: bool = True  # 第三方默认进程外


class PluginState:
    """插件运行时状态。"""

    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.status: str = "discovered"
        self.error: str = ""
        self.fail_count: int = 0
        self.last_fail_at: str = ""
        self.started_at: str = ""
        self._circuit_breaker = CircuitBreaker()

    @property
    def is_healthy(self) -> bool:
        return self._circuit_breaker.is_ok


class CircuitBreaker:
    """熔断器：连续 3 次/60s 触发熔断。"""

    def __init__(self, max_failures: int = 3, window_s: float = 60.0) -> None:
        self._max = max_failures
        self._window = window_s
        self._failures: list[float] = []

    def record_failure(self) -> None:
        now = datetime.now(UTC).timestamp()
        self._failures.append(now)
        cutoff = now - self._window
        self._failures = [t for t in self._failures if t >= cutoff]

    def record_success(self) -> None:
        if self._failures:
            self._failures.pop(0)

    @property
    def is_ok(self) -> bool:
        return len(self._failures) < self._max

    @property
    def should_disable(self) -> bool:
        return len(self._failures) >= self._max


class PluginRuntime:
    """插件生命周期管理（Plan 03 M7）。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._plugins: dict[str, PluginState] = {}

    def discover(self, *paths: str) -> list[PluginManifest]:
        """多源发现：内置、用户、项目（显式开关）、pip entry-point。"""
        # 当前阶段：返回已注册的 manifest（实际发现由部署层实现）
        return [p.manifest for p in self._plugins.values()]

    def validate(self, manifest: PluginManifest) -> bool:
        """校验 Manifest 与当前 Core API 版本兼容性。"""
        if not manifest.plugin_id or not manifest.version:
            return False
        # API 版本兼容检查
        if manifest.api_version != "1":
            return False
        return True

    def install(self, manifest: PluginManifest) -> PluginState:
        """安装插件。"""
        state = PluginState(manifest)
        state.status = "installed"
        self._plugins[manifest.plugin_id] = state
        return state

    def enable(self, plugin_id: str) -> PluginState | None:
        """启用已安装的插件。"""
        s = self._plugins.get(plugin_id)
        if not s or s.status not in ("installed", "configured", "disabled"):
            return None
        if not s.is_healthy:
            s.status = "degraded"
            return s
        s.status = "enabled"
        s.started_at = datetime.now(UTC).isoformat()
        return s

    def disable(self, plugin_id: str) -> PluginState | None:
        """禁用插件（不卸载）。"""
        s = self._plugins.get(plugin_id)
        if not s:
            return None
        s.status = "disabled"
        return s

    def record_failure(self, plugin_id: str) -> None:
        """记录失败，可能触发熔断。"""
        s = self._plugins.get(plugin_id)
        if s:
            s._circuit_breaker.record_failure()
            s.fail_count += 1
            s.last_fail_at = datetime.now(UTC).isoformat()
            if s._circuit_breaker.should_disable:
                s.status = "degraded"

    def get(self, plugin_id: str) -> PluginState | None:
        return self._plugins.get(plugin_id)

    def list_all(self) -> list[PluginState]:
        return list(self._plugins.values())
