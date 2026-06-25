"""
cogito.bus.event_bus — DomainEventBus

只发布不可变生命周期事件，Handler 不参与核心事务。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from cogito.bus.events_lifecycle import LifecycleEvent

logger = logging.getLogger(__name__)

EventHandler = Callable[[LifecycleEvent], Coroutine[Any, Any, None] | None]


class Subscription:
    """事件订阅句柄，用于取消订阅。"""

    def __init__(
        self,
        bus: "DomainEventBus",
        event_type: str,
        handler: EventHandler,
    ) -> None:
        self._bus = bus
        self._event_type = event_type
        self._handler = handler

    def unsubscribe(self) -> None:
        """取消此订阅。"""
        self._bus._remove_handler(self._event_type, self._handler)


class DomainEventBus:
    """不可变事件总线。

    规则：
    1. Event 不允许被 Handler 修改（frozen dataclass）；
    2. Event Handler 不参与核心事务；
    3. Event Handler 失败不能破坏已提交状态；
    4. 非关键事件可以异步 fanout。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def on(
        self,
        event_type: str,
        handler: EventHandler,
    ) -> Subscription:
        """订阅指定类型的事件。

        Args:
            event_type: 事件类型字符串，如 "turn_started"。
            handler: 异步回调，接收 LifecycleEvent。

        Returns:
            Subscription，可调用 unsubscribe() 取消订阅。
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        return Subscription(self, event_type, handler)

    async def publish(self, event: LifecycleEvent) -> None:
        """同步发布事件 — 等待所有 Handler 执行完毕。

        Handler 异常会被记录但不会传播，确保不破坏主流程。
        """
        event_type = event.event_type
        handlers = self._handlers.get(event_type, [])

        if not handlers:
            return

        for handler in handlers:
            try:
                result = handler(event)
                if result is not None:
                    await result
            except Exception:
                logger.exception(
                    "Event handler failed for %s (event_id=%s)",
                    event_type,
                    event.event_id,
                )

    async def publish_multi(
        self,
        events: list[LifecycleEvent],
    ) -> None:
        """批量发布事件。"""
        for event in events:
            await self.publish(event)

    def enqueue(self, event: LifecycleEvent) -> None:
        """异步发布事件 — 创建 Task 后立即返回（不等待）。

        适用于非关键的日志、调试和审计事件。
        """
        asyncio.create_task(self.publish(event))

    def _remove_handler(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            if not handlers:
                self._handlers.pop(event_type, None)
