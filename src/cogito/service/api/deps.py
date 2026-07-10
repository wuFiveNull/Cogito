"""PLAN-09 M4b: 依赖注入声明（从 interaction_web 上移到 service 层）。

应用运行时依赖关系：
- ConnProvider / CommandDeps / get_command_deps / get_conn_provider / get_runtime
  不再属于 interaction_web 的私有实现，而是 FastAPI 应用层的装配约定。
- QueryService / CommandHandlers 由具体实现依赖此装配件。

本模块 store-free：SQLite 连接由 runtime.open_conn() 在需要时打开。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from cogito.config import Config


@dataclass
class ConnProvider:
    """应用级共享：配置 + SQLite 连接工厂 + 恢复计数。"""
    config: Config
    recovery_counts: dict[str, int]

    def open_conn(self) -> sqlite3.Connection:
        from cogito.store.connection import get_connection
        return get_connection(self.config.resolve_db_path())


@dataclass
class CommandDeps:
    conn: sqlite3.Connection
    config: Config
    recovery_counts: dict[str, int]


def get_conn_provider(request: Request) -> ConnProvider:
    return request.app.state._provider  # type: ignore[attr-defined]


def get_runtime(request: Request) -> Any:
    """返回 serve 模式注入的 RuntimeApplication（聊天路由接入主链路用）。"""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail="Runtime not available (agent worker not running)",
        )
    return runtime


def get_command_deps(request: Request) -> Iterator[CommandDeps]:
    """每请求一条独立 SQLite 连接，请求结束后自动关闭 (yield 依赖)。"""
    provider = get_conn_provider(request)
    conn = provider.open_conn()
    deps = CommandDeps(
        conn=conn, config=provider.config, recovery_counts=provider.recovery_counts,
    )
    try:
        yield deps
    finally:
        try:
            conn.close()
        except Exception:
            pass
