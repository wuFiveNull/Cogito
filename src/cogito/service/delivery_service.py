"""DeliveryService —— Delivery 投递管理的唯一公开接口（PLAN-10 M4 落地）。

状态机（ACCESS-DELIVERY / 4.3）：
pending → sending → sent
              ├→ retry_scheduled
              ├→ failed
              └→ unknown → sent (reconcile)

实现：`SqliteDeliveryService`（service/sqlite_delivery_service.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class DeliveryRequest:
    """投递请求。"""

    target: dict[str, Any]
    content_ref: str
    idempotency_key: str = ""
    scheduled_at: str | None = None
    streaming: bool = False


@dataclass(frozen=True)
class DeliveryRef:
    """投递引用。"""

    delivery_id: str

    def __str__(self) -> str:
        return self.delivery_id


@dataclass(frozen=True)
class ReconcileResult:
    """reconcile 结果。"""

    delivery_id: str
    status: str  # 'sent' | 'failed' | 'still_unknown'
    platform_message_id: str | None = None


@dataclass
class DeliveryView:
    """Delivery 聚合只读视图（对齐 ACCESS-DELIVERY / 4 Delivery）。"""

    delivery_id: str = ""
    status: str = "pending"
    target_snapshot: dict[str, Any] = field(default_factory=dict)
    content_ref: str | None = None
    idempotency_key: str = ""
    attempt_count: int = 0
    platform_message_id: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    stream_version: int = 0


class DeliveryService(Protocol):
    """Delivery 投递管理接口（唯一写入口）。

    SYSTEM-BOUNDARIES / 4：Delivery 聚合唯一写入者是此接口的实现。
    共享语义：
    - 被动回复优先使用 reply_route 快照；主动发送经 DeliveryPolicy 固定 TargetSnapshot
    - unknown 只能 reconcile，不能盲目重试（GLOBAL-INVARIANT）
    - 平台调用前持久化发送意图（PLAN-10 M4）
    """

    async def enqueue(self, request: DeliveryRequest) -> DeliveryRef:
        """创建并排队一个投递请求。"""
        ...

    def get(self, delivery_id: str) -> DeliveryView | None:
        """按 ID 获取 Delivery 聚合视图。"""
        ...

    async def cancel(self, delivery_id: str, expected_version: int) -> None:
        """取消未完成的投递。"""
        ...

    async def retry(self, delivery_id: str, expected_version: int) -> None:
        """Retry only a retry_scheduled Delivery; unknown must reconcile."""
        ...

    async def reconcile(
        self,
        delivery_id: str,
        platform_message_id: str | None = None,
        *,
        confirmed: bool = False,
    ) -> ReconcileResult:
        """Ask Gateway for evidence before resolving an unknown Delivery."""
        ...
