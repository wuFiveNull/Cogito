"""PluginRuntime —— Plugin 状态的唯一公开写入口。

SYSTEM-BOUNDARIES / 4: Plugin 状态的唯一写入者是 PluginRuntime。

注意：这是前瞻性 Protocol，为 Plan 03 M7（Plugin 生命周期）铺路。
当前第三方插件生态尚未实现；Protocol 提前锁定公开面，避免后续不一致。

生命周期：discovered → validated → installed → configured → enabled → running。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class PluginManifest:
    """插件声明。"""
    plugin_id: str
    version: str
    api_version: str
    permissions: list[str]
    entry_point: str = ""
    config_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class PluginState:
    """插件运行时状态。"""
    plugin_id: str
    status: str
    # status enum:
    # discovered|validated|installed|configured|enabled|running|degraded|disabled|stopped
    manifest: PluginManifest
    error: str = ""


class PluginRuntime(Protocol):
    """插件生命周期管理接口。

    唯一写入口：所有 Plugin 的状态变更经此接口。
    Core 私有模块、数据库、Secret 默认对 Plugin 不可见。
    """

    def discover(self, *paths: str) -> list[PluginManifest]:
        """发现插件，返回候选 Manifest 列表。"""
        ...

    def validate(self, manifest: PluginManifest) -> bool:
        """校验 Manifest 与当前 Core API 版本兼容性。"""
        ...

    def enable(self, plugin_id: str) -> PluginState:
        """启用已安装的插件。"""
        ...

    def disable(self, plugin_id: str) -> PluginState:
        """禁用插件（不卸载）。"""
        ...

    def get(self, plugin_id: str) -> PluginState | None:
        """获取插件当前状态。"""
        ...

    def list(self) -> list[PluginState]:
        """列出所有已知插件。"""
        ...
