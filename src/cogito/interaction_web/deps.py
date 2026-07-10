"""PLAN-09 M4b: interaction_web/deps 现在只是 service/api/deps 的转发层。

保持向后兼容：外部代码 `from cogito.interaction_web.deps import CommandDeps`
继续可用。
"""
from __future__ import annotations

from cogito.service.api.deps import (  # noqa: F401
    CommandDeps,
    ConnProvider,
    get_command_deps,
    get_conn_provider,
    get_runtime,
)

__all__ = [
    "ConnProvider",
    "CommandDeps",
    "get_conn_provider",
    "get_runtime",
    "get_command_deps",
]
