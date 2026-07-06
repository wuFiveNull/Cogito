"""
cogito.database.connection — 异步 SQLite 连接管理

提供 AsyncDatabase 类，封装 aiosqlite，自动配置 WAL 模式 PRAGMA。

使用方式：
    db = await AsyncDatabase.open(".workspace/cogito.db")
    try:
        await db.execute("SELECT 1")
    finally:
        await db.close()
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

from cogito.database.schema import CONFIG_SQL

logger = logging.getLogger(__name__)


class AsyncDatabase:
    """异步 SQLite 数据库连接管理器。

    封装 aiosqlite 连接，自动应用 PRAGMA 配置。
    单用户场景使用单连接 + busy_timeout 即可。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._db_path

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def open(self) -> None:
        """打开数据库连接并应用 PRAGMA 配置。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(
            database=str(self._db_path),
            check_same_thread=False,
        )

        # 启用 row_factory 支持按列名访问
        self._conn.row_factory = aiosqlite.Row

        # 检查 SQLite 版本
        version = await self._scalar("SELECT sqlite_version()")
        logger.info("Database opened: %s (sqlite_version=%s)", self._db_path, version)
        _check_sqlite_version(version)

        # 应用 PRAGMA 配置
        await self._conn.executescript(CONFIG_SQL)

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Database closed: %s", self._db_path)

    async def execute(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
    ) -> aiosqlite.Cursor:
        """执行单条 SQL。"""
        self._ensure_connected()
        if parameters:
            return await self._conn.execute(sql, parameters)
        return await self._conn.execute(sql)

    async def executemany(
        self,
        sql: str,
        parameters: list[dict[str, Any]],
    ) -> aiosqlite.Cursor:
        """批量执行 SQL。"""
        self._ensure_connected()
        return await self._conn.executemany(sql, parameters)

    async def executescript(self, sql: str) -> None:
        """执行多语句 SQL（无参数绑定）。"""
        self._ensure_connected()
        await self._conn.executescript(sql)

    async def fetchone(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """查询单行，返回 dict 或 None。"""
        cursor = await self.execute(sql, parameters)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetchall(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """查询多行，返回 list[dict]。"""
        cursor = await self.execute(sql, parameters)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def fetchcol(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[Any]:
        """查询单列，返回 list。"""
        cursor = await self.execute(sql, parameters)
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def _scalar(self, sql: str) -> Any:
        """查询标量值。"""
        cursor = await self._conn.execute(sql)
        row = await cursor.fetchone()
        return row[0] if row else None

    async def changes(self) -> int:
        """返回最近一次 INSERT/UPDATE/DELETE 影响的行数。"""
        return await self._scalar("SELECT changes()")

    async def begin_immediate(self) -> None:
        """以 IMMEDIATE 模式开始事务（提前获取写锁）。

        安全处理已经处于事务中的情况（例如 aiosqlite 在 WAL 模式下
        自动开启的隐式事务）。如果已经在一个事务中，此方法无害通过。
        """
        self._ensure_connected()
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
        except aiosqlite.OperationalError as exc:
            if "cannot start a transaction within a transaction" in str(exc):
                # 已经处于事务中 — 这是安全的，无需处理。
                logger.debug("Transaction already active; begin_immediate skipped")
            else:
                raise

    async def commit(self) -> None:
        """提交当前事务。"""
        self._ensure_connected()
        await self._conn.commit()

    async def rollback(self) -> None:
        """回滚当前事务。"""
        self._ensure_connected()
        await self._conn.rollback()

    async def execute_in_transaction(
        self,
        statements: list[tuple[str, dict[str, Any] | None]],
    ) -> None:
        """在一个 IMMEDIATE 事务中执行多条语句。

        Args:
            statements: [(sql, parameters), ...] 参数为 None 表示无参数
        """
        try:
            await self.begin_immediate()
            for sql, params in statements:
                await self.execute(sql, params)
            await self.commit()
        except Exception:
            await self.rollback()
            raise

    def _ensure_connected(self) -> None:
        if self._conn is None:
            raise RuntimeError(
                f"Database not opened: {self._db_path}. "
                "Call await db.open() first."
            )


_MIN_SQLITE_VERSION = (3, 51, 3)


def _parse_version(version_str: str) -> tuple[int, int, int]:
    """解析 '3.51.3' 或 '3.51.3-xxx' 格式的版本字符串。"""
    parts = version_str.split("-")[0].split(".")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def _check_sqlite_version(version: str) -> None:
    """检查 SQLite 版本，低于推荐值则发出警告。"""
    current = _parse_version(version)
    if current < _MIN_SQLITE_VERSION:
        logger.warning(
            "SQLite version %s is below recommended %s. "
            "Consider upgrading to avoid WAL/checkpoint issues.",
            version,
            ".".join(str(v) for v in _MIN_SQLITE_VERSION),
        )
