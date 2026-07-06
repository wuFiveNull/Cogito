"""DeliveryService protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class DeliveryRequest:
    """投递请求。"""
    target: dict[str, Any]
    content_ref: str
    idempotency_key: str = ""
    scheduled_at: str | None = None


@dataclass
class DeliveryRef:
    """投递引用。"""
    delivery_id: str


class DeliveryService(Protocol):
    """Delivery 投递管理接口。"""

    async def enqueue(self, request: DeliveryRequest) -> DeliveryRef:
        """创建并排队一个投递请求。"""
        ...

    async def cancel(self, delivery_id: str) -> None:
        """取消未完成的投递。"""
        ...

    async def retry(self, delivery_id: str) -> None:
        """重试失败的投递。"""
        ...
