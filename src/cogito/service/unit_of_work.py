"""Unit of Work — transaction management for Cogito services.

提供事务边界管理，所有 Repository 共享同一连接。
利用 SQLite 隐式事务，不嵌套 BEGIN。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from cogito.contracts.memory import MemoryWriter
from cogito.domain.memory import MemoryItem
from cogito.service.memory_service import SqliteMemoryService
from cogito.store.memory_repo import MemoryRepository
from cogito.store.repositories import (
    ConversationRepository,
    EndpointRepository,
    InboxRepository,
    MessageRepository,
    OutboxRepository,
    PrincipalRepository,
    SessionRepository,
    TurnRepository,
)

# ── PLAN-09 M4a: MemoryWriter adapter that wraps a UnitOfWork ──
# Tools call MemoryWriter.remember/forget/... through this adapter; each
# call creates its own short connection + UnitOfWork + commit, so tool
# files never see SqliteMemoryService or service.unit_of_work directly.


class UnitOfWorkMemoryWriter:
    """MemoryWriter 端口适配器：每次写操作独立 UoW + commit。

    供 tools/*.py 的独立连接路径使用（make_memory_writer 工厂），
    保证 never 跨工具调用串事务。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(self._conn)

    def remember(
        self,
        kind: str,
        subject: str,
        predicate: str,
        value: str,
        principal_id: str,
        scope_type: str = "",
        scope_id: str = "",
        scope: str = "",
        source_type: str = "message",
        source_id: str = "",
        explicitness: str = "explicit_user_statement",
        confidence: float = 1.0,
        importance: float = 0.7,
    ) -> MemoryItem:
        with self._uow() as uow:
            svc = uow.memory_service
            result = svc.remember(
                kind=kind, subject=subject, predicate=predicate, value=value,
                principal_id=principal_id,
                scope_type=scope_type, scope_id=scope_id, scope=scope,
                source_type=source_type, source_id=source_id,
                explicitness=explicitness, confidence=confidence,
                importance=importance,
            )
            uow.commit()
            return result

    def forget(self, memory_id: str, principal_id: str = "") -> bool:
        with self._uow() as uow:
            svc = uow.memory_service
            result = svc.forget(memory_id, principal_id=principal_id)
            if result:
                uow.commit()
            return result

    def forget_by_canonical_key(
        self, principal_id: str, subject: str, predicate: str,
    ) -> bool:
        with self._uow() as uow:
            svc = uow.memory_service
            result = svc.forget_by_canonical_key(principal_id, subject, predicate)
            if result:
                uow.commit()
            return result

    def confirm(self, memory_id: str, confirmed_by: str = "") -> bool:
        with self._uow() as uow:
            svc = uow.memory_service
            result = svc.confirm(memory_id, confirmed_by=confirmed_by)
            if result:
                uow.commit()
            return result

    def reject(self, memory_id: str, principal_id: str = "") -> bool:
        with self._uow() as uow:
            svc = uow.memory_service
            result = svc.reject(memory_id, principal_id=principal_id)
            if result:
                uow.commit()
            return result


def make_unit_of_work_memory_writer(
    db_path: str,
) -> MemoryWriter:
    """工厂：创建一个依赖"新鲜连接 + 独立 UoW"的事务性 MemoryWriter。

    供 tools/*.py 的记忆写工具使用；工具文件无需知道
    SqliteMemoryService / UnitOfWork 的存在。
    """
    from cogito.store.connection import get_connection as _get_conn
    conn = _get_conn(db_path)
    return UnitOfWorkMemoryWriter(conn=conn)


class UnitOfWork:
    """工作单元 —— 管理数据库事务和 Repository 访问。

    SQLite 默认 autocommit=True，第一条 DML 自动开始隐式事务。
    commit/rollback 结束当前事务，后续 DML 开始新事务。

    用法:
        with UnitOfWork(conn) as uow:
            inbox = uow.inbox.find(...)
            uow.commit()
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._committed = False

        self._inbox: InboxRepository | None = None
        self._principal: PrincipalRepository | None = None
        self._endpoint: EndpointRepository | None = None
        self._conversation: ConversationRepository | None = None
        self._session: SessionRepository | None = None
        self._message: MessageRepository | None = None
        self._turn: TurnRepository | None = None
        self._outbox: OutboxRepository | None = None
        self._memory: MemoryRepository | None = None
        self._memory_service: SqliteMemoryService | None = None

    @property
    def inbox(self) -> InboxRepository:
        if self._inbox is None:
            self._inbox = InboxRepository(self._conn)
        return self._inbox

    @property
    def principal(self) -> PrincipalRepository:
        if self._principal is None:
            self._principal = PrincipalRepository(self._conn)
        return self._principal

    @property
    def endpoint(self) -> EndpointRepository:
        if self._endpoint is None:
            self._endpoint = EndpointRepository(self._conn)
        return self._endpoint

    @property
    def conversation(self) -> ConversationRepository:
        if self._conversation is None:
            self._conversation = ConversationRepository(self._conn)
        return self._conversation

    @property
    def session(self) -> SessionRepository:
        if self._session is None:
            self._session = SessionRepository(self._conn)
        return self._session

    @property
    def message(self) -> MessageRepository:
        if self._message is None:
            self._message = MessageRepository(self._conn)
        return self._message

    @property
    def turn(self) -> TurnRepository:
        if self._turn is None:
            self._turn = TurnRepository(self._conn)
        return self._turn

    @property
    def outbox(self) -> OutboxRepository:
        if self._outbox is None:
            self._outbox = OutboxRepository(self._conn)
        return self._outbox

    @property
    def memory(self) -> MemoryRepository:
        if self._memory is None:
            self._memory = MemoryRepository(self._conn)
        return self._memory

    @property
    def memory_service(self) -> SqliteMemoryService:
        """返回共享同一事务的 MemoryService。"""
        if self._memory_service is None:
            self._memory_service = SqliteMemoryService(repo=self.memory)
        return self._memory_service

    # ── 事务边界 ──

    def begin(self) -> None:
        """不做任何操作 —— 第一条 DML 会触发 SQLite 隐式事务。"""
        pass

    def commit(self) -> None:
        self._conn.commit()
        self._committed = True

    def rollback(self) -> None:
        self._conn.rollback()

    def __enter__(self) -> UnitOfWork:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None and not self._committed:
            # 进入但没有 commit → 回滚
            self._conn.rollback()
        elif exc_type is not None:
            # 异常 → 回滚
            try:
                self._conn.rollback()
            except Exception:
                pass
