"""
cogito.delivery.manager — DeliveryManager

统一出站投递管理。所有出站（被动回复、主动推送、工具调用）
统一经过 DeliveryManager，复用持久化、重试、权限和可观测机制。

当前实现：
  - 最小版本，直接将出站请求路由到 ChannelRegistry 中的 Channel。
  - 每步发布 LifecycleEvent 到 DomainEventBus 供可观测性消费。

后续增加：
  - Outbox 持久化 (Transactional Outbox)
  - 每 Channel 独立队列 + Worker
  - 结构化重试 (RetryScheduler)
  - 投递状态跟踪 (accepted → delivered / retrying / failed / dead)
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from cogito.bus.events import DeliveryReceipt, OutboundRequest
from cogito.bus.event_bus import DomainEventBus
from cogito.bus.events_lifecycle import (
    DeliveryFailed,
    DeliveryStarted,
    DeliverySucceeded,
    LifecycleEvent,
    OutboundAccepted,
)
from cogito.channels.registry import ChannelRegistry

logger = logging.getLogger(__name__)


def _new_event_id() -> str:
    return uuid4().hex


class DeliveryManager:
    """统一出站投递管理。"""

    def __init__(
        self,
        registry: ChannelRegistry,
        domain_bus: DomainEventBus | None = None,
    ) -> None:
        self._registry = registry
        self._domain_bus = domain_bus

    async def submit(
        self,
        request: OutboundRequest,
    ) -> DeliveryReceipt:
        """提交一个出站请求，立即尝试投递。

        生命周期事件流：
          outbound_accepted → delivery_started → delivery_succeeded
                                               → delivery_failed
        """
        trace_id = request.trace_id
        outbound_id = request.outbound_id

        # 1. outbound_accepted
        await self._publish(
            OutboundAccepted(
                event_id=_new_event_id(),
                trace_id=trace_id,
                session_key=request.session_key,
                turn_id=request.turn_id,
                outbound_id=outbound_id,
            ),
        )

        try:
            channel = self._registry.get(request.channel)
        except KeyError:
            logger.warning(
                "No channel registered for %r; outbound dropped",
                request.channel,
                extra={
                    "outbound_id": outbound_id,
                    "channel": request.channel,
                },
            )
            receipt = DeliveryReceipt(
                outbound_id=outbound_id,
                status="failed",
                attempts=1,
                error_code="UNKNOWN_CHANNEL",
                error_message=f"No channel registered: {request.channel}",
            )
            await self._publish(
                DeliveryFailed(
                    event_id=_new_event_id(),
                    trace_id=trace_id,
                    session_key=request.session_key,
                    turn_id=request.turn_id,
                    outbound_id=outbound_id,
                    error_code=receipt.error_code or "",
                    error_message=receipt.error_message or "",
                ),
            )
            return receipt

        # 2. delivery_started
        await self._publish(
            DeliveryStarted(
                event_id=_new_event_id(),
                trace_id=trace_id,
                session_key=request.session_key,
                turn_id=request.turn_id,
                outbound_id=outbound_id,
            ),
        )

        try:
            channel_receipt = await channel.send(request)

            # 3a. delivery_succeeded
            await self._publish(
                DeliverySucceeded(
                    event_id=_new_event_id(),
                    trace_id=trace_id,
                    session_key=request.session_key,
                    turn_id=request.turn_id,
                    outbound_id=outbound_id,
                    external_message_id=channel_receipt.external_message_id or "",
                ),
            )

            return channel_receipt

        except Exception as exc:
            logger.exception(
                "Delivery failed",
                extra={
                    "outbound_id": outbound_id,
                    "channel": request.channel,
                },
            )

            # 3b. delivery_failed
            await self._publish(
                DeliveryFailed(
                    event_id=_new_event_id(),
                    trace_id=trace_id,
                    session_key=request.session_key,
                    turn_id=request.turn_id,
                    outbound_id=outbound_id,
                    error_code=exc.__class__.__name__,
                    error_message=str(exc)[:500],
                ),
            )

            return DeliveryReceipt(
                outbound_id=outbound_id,
                status="failed",
                attempts=1,
                error_code=exc.__class__.__name__,
                error_message=str(exc)[:500],
            )

    async def _publish(self, event: LifecycleEvent) -> None:
        if self._domain_bus is None:
            return
        try:
            await self._domain_bus.publish(event)
        except Exception:
            logger.exception(
                "Failed to publish delivery lifecycle event",
                extra={"event_type": event.event_type},
            )
