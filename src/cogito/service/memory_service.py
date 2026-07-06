"""MemoryService protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class MemoryQuery:
    """Memory 检索请求。"""
    scope: str = ""
    kinds: list[str] | None = None
    query_text: str = ""
    max_results: int = 10
    min_confidence: float = 0.0
    filter_status: str | None = None


@dataclass
class MemoryResult:
    """Memory 检索结果。"""
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0


@dataclass
class MemoryCandidate:
    """Memory 候选提议。"""
    kind: str = "fact"
    subject: str = ""
    predicate: str = ""
    value: str = ""
    confidence: float = 0.5
    source_type: str = ""
    source_id: str = ""


class MemoryService(Protocol):
    """Memory 生命周期管理接口。"""

    async def retrieve(self, query: MemoryQuery) -> MemoryResult:
        """检索符合条件的 MemoryItem。"""
        ...

    async def propose(self, candidates: list[MemoryCandidate]) -> list[dict[str, Any]]:
        """提议新的 MemoryItem（进入 candidate 状态）。"""
        ...

    async def confirm(self, memory_id: str) -> dict[str, Any]:
        """确认 MemoryItem（candidate → confirmed）。"""
        ...

    async def reject(self, memory_id: str) -> None:
        """拒绝 MemoryItem（candidate → rejected）。"""
        ...
