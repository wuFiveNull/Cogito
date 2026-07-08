"""DeliveryService —— Delivery 投递管理的唯一公开接口（Phase 1.5 共享契约）。

Plan 04 M8 / Plan 05 M7 共同依赖此接口的 reconcile 与 streaming 语义。
实现：`SqliteDeliveryService`（SQLite 后端 + Gateway 集成）。

状态机（ACCESS-DELIVERY / 4.3）：
pending → sending → sent
              ├→ retry_scheduled
              ├→ failed
              └→ unknown → sent (reconcile)
"""
from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class ReconcileResult:
    """reconcile 结果。"""
    delivery_id: str
    status: str  # 'sent' | 'failed' | 'still_unknown'
    platform_message_id: str | None = None


class DeliveryService(Protocol):
    """Delivery 投递管理接口（唯一写入口）。

    共享语义：
    - 被动回复优先使用 reply_route 快照；主动发送经 DeliveryPolicy 固定 TargetSnapshot
    - unknown 只能 reconcile，不能盲目重试（GLOBAL-INVARIANT）
    - 平台调用前持久化发送意图（Plan 05 M7）
    """

    async def enqueue(self, request: DeliveryRequest) -> DeliveryRef:
        """创建并排队一个投递请求。"""
        ...

    async def cancel(self, delivery_id: str) -> None:
        """取消未完成的投递。"""
        ...

    async def retry(self, delivery_id: str) -> None:
        """重试 retry_scheduled 的投递。"""
        ...

    async def reconcile(
        self, delivery_id: str, platform_message_id: str | None = None,
    ) -> ReconcileResult:
        """对 unknown 状态的投递做对账确认（Plan 05 M7）。"""
        ...

    def get(self, delivery_id: str) -> dict[str, Any] | None:
        """按 ID 获取 Delivery 原始记录。"""
        ...


# NOTE: SqliteDeliveryService concrete implementation is deferred to Phase 2
# Track C/D (interaction + background), which build the actual enqueue/reconcile
# flow atop the existing delivery_worker + Gateway architecture. The Protocol
# above is the shared contract both tracks program against.
