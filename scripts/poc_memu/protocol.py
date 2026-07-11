"""Backend 对比协议（PLAN-13 P13-15）。

所有后端（Cogito / memU）实现同一协议，保证数据集和指标可比。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KnowledgeBackend(Protocol):
    """知识检索后端协议。"""

    def ingest(self, doc_id: str, content: str) -> list[str]:
        """摄入文档，返回段落地 ID 列表。"""
        ...

    def retrieve(self, query: str, top_k: int = 8) -> list[tuple[str, float]]:
        """检索，返回 (段落地 ID, score) 列表。"""
        ...

    def invalidate(self, doc_id: str) -> None:
        """撤销文档的所有段落地（删除/失效）。"""
        ...

    def segment_provenance(self, segment_id: str) -> str | None:
        """返回段落地来源链（None 表示不可追溯）。"""
        ...
