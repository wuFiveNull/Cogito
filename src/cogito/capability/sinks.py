"""Sink adapters — bridge concrete store repos to Port contracts (PLAN-09 M4a).

capability/sinks.py 是使用 store 实现的唯一 capability 层入口，
让 capability 其它模块（executor / registry / models）只依赖 contracts 端口。
"""
from __future__ import annotations

from cogito.contracts.tool_call import ToolCallSink
from cogito.store.tool_call_repo import ToolCallRepository


class ToolCallRepositorySink:
    """ToolCallSink 的 ToolCallRepository 薄包装。

    executor._persist_start 传入完整记录（insert）；
    executor._persist_end 传入精简记录（update_status）。
    通过 record 里是否含 started_at 区分语义。
    """

    def __init__(self, repo: ToolCallRepository) -> None:
        self._repo = repo

    def insert(self, record: object) -> None:
        rec = record if isinstance(record, dict) else {}
        if "started_at" in rec:
            # 完整记录 → insert
            self._repo.insert(rec)  # type: ignore[arg-type]
        else:
            # 精简记录 → update_status
            self._repo.update_status(
                rec.get("tool_call_id", ""),
                rec.get("status", ""),
                result_summary=rec.get("result_summary", ""),
                completed_at=rec.get("completed_at"),
            )
