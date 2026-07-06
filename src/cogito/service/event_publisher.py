"""EventPublisher protocol."""

from __future__ import annotations

from typing import Protocol

from cogito.domain.events import DomainEvent


class EventPublisher(Protocol):
    """Event 发布接口（将 Event 加入当前 Unit of Work）。"""

    def publish(self, event: DomainEvent) -> None:
        """发布一个领域事件。

        该调用只将 Event 加入当前 Unit of Work，由 Outbox 在事务提交后投递。
        """
        ...
