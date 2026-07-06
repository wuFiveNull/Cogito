"""
cogito.bus.inbound — 入站总线与入站端口

InboundBus 只负责标准入站工作项的排队。

Channel 只依赖 InboundPort 这一最小入站接口。
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from cogito.bus.events import InboundItem


class InboundPort(Protocol):
    """Channel 依赖的最小入站接口。"""

    async def publish(self, item: InboundItem) -> None:
        """提交一个入站项到总线。"""


class InboundBus:
    """有界异步队列，作为入站消息的统一入口。

    队列必须有上限，避免 Provider 或工具长时间阻塞后入站无限堆积。
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._queue: asyncio.Queue[InboundItem] = asyncio.Queue(
            maxsize=maxsize,
        )

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    @property
    def qsize(self) -> int:
        """当前队列中的消息数量（主要用于监控，不用于判断）。"""
        return self._queue.qsize()

    async def publish(self, item: InboundItem) -> None:
        """将一个入站项放入队列，队列满时阻塞。"""
        await self._queue.put(item)

    async def consume(self) -> InboundItem:
        """从队列取出一个入站项，队列空时阻塞。"""
        return await self._queue.get()

    def task_done(self) -> None:
        """标记上一个取出的项已被处理完成。"""
        self._queue.task_done()

    async def join(self) -> None:
        """等待队列中所有项被处理完毕。"""
        await self._queue.join()
