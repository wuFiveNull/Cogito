"""MemoryReader / MemoryWriter — 长期记忆端口 (PLAN-09 M3/M4a).

Tools 层（tools/*.py）通过此 Protocol 消费长期记忆，
不依赖 `service.memory_service.SqliteMemoryService` 具体实现。

实现：`service.memory_service.SqliteMemoryService`（现在同时实现
MemoryReader + MemoryWriter）。
"""
from __future__ import annotations

from typing import Protocol

from cogito.domain.memory import MemoryItem


class MemoryReader(Protocol):
    """长期记忆检索入口。

    DOMAIN-CONTRACTS / 1.13 MemoryItem
    PLAN-09 / §3.2：runtime 层不得直接依赖 SqliteMemoryService；
    所有记忆检索经由此 Protocol，由组合根在 service 层实现注入。
    """

    def retrieve(
        self,
        principal_id: str,
        query: str = "",
        scope_type: str = "",
        scope_id: str = "",
        kinds: list[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        """检索有效记忆。"""
        ...

    def get(self, memory_id: str) -> MemoryItem | None:
        """按 ID 获取记忆。"""
        ...


class MemoryWriter(Protocol):
    """长期记忆写入入口。

    DOMAIN-CONTRACTS / 1.13 MemoryItem
    PLAN-09 / M4a：tools 层写记忆只能通过此 Protocol，
    不直接依赖 SqliteMemoryService。
    """

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
        """幂等写入记忆。"""
        ...

    def forget(self, memory_id: str, principal_id: str = "") -> bool:
        """按 ID 忘记一条记忆。"""
        ...

    def forget_by_canonical_key(
        self, principal_id: str, subject: str, predicate: str,
    ) -> bool:
        """按 canonical key 忘记一条记忆。"""
        ...

    def confirm(self, memory_id: str, confirmed_by: str = "") -> bool:
        """确认候选记忆。"""
        ...

    def reject(self, memory_id: str, principal_id: str = "") -> bool:
        """拒绝候选记忆。"""
        ...
