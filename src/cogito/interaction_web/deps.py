"""interaction-web 依赖注入声明。

把 CommandDeps 与依赖工厂独立到此模块，避免 server ↔ commands/query 循环导入。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

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
