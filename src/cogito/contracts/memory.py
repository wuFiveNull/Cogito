"""MemoryReader Protocol — 长期记忆检索端口 (PLAN-09 M3).

Runtime 层和 capability 层通过此 Protocol 消费长期记忆，
不依赖 `service.memory_service.SqliteMemoryService` 具体实现。

实现：`service.memory_service.SqliteMemoryService`。
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
