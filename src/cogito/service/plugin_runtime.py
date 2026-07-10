"""PluginRuntime 公开面 — 从 capability.plugin_runtime 重导出 (PLAN-10 M5 合并)。

system/capability 间依赖方向：service → capability ✅（允许）；
capability → service ❌（循环）。本模块仅重导出，不重新定义，
避免 capability 反向依赖 service。

真实 Protocol + 实现均在 capability/plugin_runtime.py。
"""
from cogito.capability.plugin_runtime import (  # noqa: F401
    CircuitBreaker,
    PluginManifest,
    PluginRuntime,
    PluginState,
    SqlitePluginRuntime,
)
from cogito.capability.plugin_supervisor import (  # noqa: F401
    PluginPermissionError,
    PluginPolicyAdapter,
    PluginProcessSupervisor,
)

__all__ = [
    "PluginManifest",
    "PluginState",
    "PluginRuntime",
    "CircuitBreaker",
    "SqlitePluginRuntime",
    "PluginPermissionError",
    "PluginPolicyAdapter",
    "PluginProcessSupervisor",
]
