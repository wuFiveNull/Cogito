"""
cogito.database.manager — DatabaseManager

顶层数据库管理器，聚合所有 Repository 和 Service。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from cogito.database.connection import AsyncDatabase
from cogito.database.migrations import run_migrations
from cogito.database.repository.events import EventRepository
from cogito.database.repository.memories import MemoryRepository
from cogito.database.repository.trace_events import TraceEventRepository
from cogito.database.service.event_service import EventService
from cogito.database.service.memory_retriever import MemoryRetriever
from cogito.database.service.memory_writer import MemoryWriter
from cogito.database.service.trace_service import TraceService

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class DatabaseManager:
    """数据库管理器，聚合所有数据访问和业务服务。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db = AsyncDatabase(db_path)
        self._migrated = False

        # Repository (数据访问层)
        self.trace_events = TraceEventRepository(self._db)
        self.events = EventRepository(self._db)
        self.memories = MemoryRepository(self._db)

        # Service (业务逻辑层)
        self.trace = TraceService(self._db)
        self.event = EventService(self._db)
        self.memory_writer = MemoryWriter(self._db)
        self.memory_retriever = MemoryRetriever(self._db)

    @property
    def db(self) -> AsyncDatabase:
        return self._db

    @property
    def db_path(self) -> Path:
        return self._db.path

    async def open(self) -> None:
        """打开数据库连接并执行迁移。"""
        await self._db.open()

        if not self._migrated:
            version = await run_migrations(self._db)
            self._migrated = True
            logger.info("Database ready (schema v%s)", version)

    async def close(self) -> None:
        """关闭数据库连接。"""
        await self._db.close()
        self._migrated = False

    async def health_check(self) -> bool:
        """健康检查：执行简单查询确认数据库可用。"""
        try:
            row = await self._db.fetchone("SELECT 1 AS ok")
            return row is not None and row.get("ok") == 1
        except Exception:
            return False

    # ── 生命周期钩子（供 Application 调用） ──────────────────────

    def on_turn_started(self) -> Callable:
        """返回一个可注册到 Bus 的处理器，在 turn 开始时创建根 span。

        用法:
            bus.on("turn_started", db_manager.on_turn_started())
        """
        from cogito.bus.events_lifecycle import TurnStarted

        async def handler(event: TurnStarted) -> None:
            await self.trace.create_span(
                trace_id=event.trace_id,
                user_id=event.session_key or "unknown",
                session_id=event.session_key,
                step_type="segment",
                step_name="agent_request",
                span_id=event.turn_id,
            )

        return handler

    def on_turn_committed(self) -> Callable:
        """返回处理器，在 turn 提交时完成根 span。"""
        from cogito.bus.events_lifecycle import TurnCommitted

        async def handler(event: TurnCommitted) -> None:
            span = await self.trace._repo.get_by_id(
                event.turn_id or ""
            )
            if span and span.get("status") == "running":
                await self.trace.complete_span(
                    span["id"],
                    status="success",
                )

        return handler
