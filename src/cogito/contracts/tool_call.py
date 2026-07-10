"""ToolCallSink — tool 执行持久化端口 (PLAN-09 M4a/V5).

capability.executor 通过此端口持久化 tool-call 记录；
具体实现：store.tool_call_repo.ToolCallRepository。
"""
from __future__ import annotations

from typing import Protocol


class ToolCallSink(Protocol):
    """Tool 调用持久化入口。

    capability.executor 在 tool-call 开始/结束时各调用一次。
    实现需幂等、非阻塞（异常吞掉，由 executor 上层的 try/except 兜底）。
    """

    def insert(self, record: object) -> None:
        """插入一条 tool-call 记录。"""
        ...
