"""
cogito.turns.runner — TurnRunner

入站消息与 Agent Runtime 之间的核心桥梁。

流程:
  InboundMessage
    → 映射为 AgentRequest
    → AgentApplicationService.process()
    → TurnResult 映射为 OutboundRequest
    → DeliveryManager.submit()

Bridge 角色:
  - 依赖 InboundMessage (cogito.bus.events)
  - 依赖 AgentApplicationService (cogito.agent.application)
  - 依赖 DeliveryManager (cogito.delivery)
  - 不依赖 Channel 具体实现
  - Kernel 不感知此模块存在
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from cogito.agent.application.agent_service import AgentApplicationService
from cogito.agent.ports.domain_event_bus_sink import (
    DomainEventBusAgentEventSink,
)
from cogito.agent.runtime.models import AgentRequest
from cogito.bus.event_bus import DomainEventBus
from cogito.bus.events import (
    DeliveryReceipt,
    InboundMessage,
    MessagePayload,
    OutboundRequest,
    TextPart,
)
from cogito.bus.events_lifecycle import (
    LifecycleEvent,
    TurnFailed,
    TurnStarted,
)
from cogito.delivery.manager import DeliveryManager

if TYPE_CHECKING:
    from cogito.agent.ports.events import AgentEventSink

logger = logging.getLogger(__name__)


def _inbound_to_request(msg: InboundMessage) -> AgentRequest:
    """Map an InboundMessage to an AgentRequest."""
    # Extract text from payload parts
    text_parts = [
        p.text for p in msg.payload.parts if isinstance(p, TextPart)
    ]
    text = "\n".join(text_parts)

    return AgentRequest(
        request_id=msg.message_id,
        session_id=msg.session_key,
        actor_id=f"{msg.channel}:{msg.target}",
        text=text,
        metadata={
            "trace_id": msg.trace_id,
            "channel": msg.channel,
            "target": msg.target,
            "reply_to": msg.reply_to or "",
        },
    )


def _result_to_outbound(
    result,
    *,
    original_msg: InboundMessage,
) -> OutboundRequest:
    """Map an Agent TurnResult back to an outbound message."""
    return OutboundRequest(
        outbound_id=result.turn_id,
        channel=original_msg.channel,
        target=original_msg.target,
        payload=MessagePayload(
            parts=[TextPart(text=result.text)],
        ),
        origin="reply",
        trace_id=original_msg.trace_id,
        session_key=original_msg.session_key,
        turn_id=result.turn_id,
        created_at=datetime.now(),
    )


def _error_to_outbound(
    error_text: str,
    *,
    original_msg: InboundMessage,
) -> OutboundRequest:
    """Create an error outbound message when the turn fails."""
    import uuid
    error_id = uuid.uuid4().hex
    return OutboundRequest(
        outbound_id=error_id,
        channel=original_msg.channel,
        target=original_msg.target,
        payload=MessagePayload(
            parts=[TextPart(text=error_text)],
        ),
        origin="reply",
        trace_id=original_msg.trace_id,
        session_key=original_msg.session_key,
        turn_id=error_id,
        created_at=datetime.now(),
    )


def _new_event_id() -> str:
    import uuid
    return uuid.uuid4().hex


LIFECYCLE_EVENT_TYPE_MAP = {
    "turn_started": "turn_started",
    "turn_completed": "turn_completed",
    "turn_failed": "turn_failed",
    "phase_started": "phase_started",
    "phase_completed": "phase_completed",
    "phase_failed": "phase_failed",
    "model_call_started": "llm_call_started",
    "model_call_completed": "llm_call_completed",
    "model_delta": "model_delta",
    "tool_call_started": "tool_call_started",
    "tool_call_completed": "tool_call_completed",
    "tool_call_failed": "tool_call_failed",
    "retrieval_started": "retrieval_started",
    "retrieval_completed": "retrieval_completed",
    "knowledge_extracted": "knowledge_extracted",
    "persistence_completed": "persistence_completed",
}


class TurnRunner:
    """Bridges inbound messages to the Agent Runtime and back to delivery.

    This is the TurnRunner described in message-system-plan.md §8.
    It owns the mapping between bus-level messages and agent-level requests,
    and orchestrates the complete turn lifecycle with hooks and events.
    """

    def __init__(
        self,
        *,
        service: AgentApplicationService,
        delivery: DeliveryManager,
        domain_bus: DomainEventBus | None = None,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self._service = service
        self._delivery = delivery
        self._domain_bus = domain_bus
        self._event_sink = event_sink

    async def run(self, msg: InboundMessage) -> None:
        """Execute one turn from an inbound message."""
        trace_id = msg.trace_id

        # 1. Lifecycle: TurnStarted
        await self._publish_lifecycle(
            TurnStarted(
                event_id=_new_event_id(),
                trace_id=trace_id,
                session_key=msg.session_key,
                message_id=msg.message_id,
            ),
        )

        try:
            # 2. Map inbound → AgentRequest
            request = _inbound_to_request(msg)
            logger.debug("TurnRunner request: text=%r session=%s", request.text[:100] if request.text else "", request.session_id)

            # 3. Build the event sink that bridges agent events to the bus
            sink = await self._build_event_sink(msg)

            # 4. Process through the agent runtime
            result = await self._service.process(
                request,
                event_sink=sink,
            )

            # 5. Map result → outbound
            outbound = _result_to_outbound(
                result,
                original_msg=msg,
            )

            # 6. Queue for delivery
            await self._delivery.submit(outbound)

            # 7. Lifecycle: turn completed
            await self._publish_lifecycle(
                LifecycleEvent(
                    event_id=_new_event_id(),
                    event_type="turn_completed",
                    trace_id=trace_id,
                    session_key=msg.session_key,
                    turn_id=result.turn_id,
                    message_id=msg.message_id,
                ),
            )

        except Exception as exc:
            logger.exception(
                "Turn failed",
                extra={
                    "trace_id": trace_id,
                    "message_id": msg.message_id,
                },
            )

            # Send error response back through delivery manager
            try:
                error_text = f"处理消息时出错：{exc}"
                error_outbound = _error_to_outbound(
                    error_text,
                    original_msg=msg,
                )
                await self._delivery.submit(error_outbound)
            except Exception:
                logger.exception(
                    "Failed to submit error outbound",
                    extra={"trace_id": trace_id},
                )

            await self._publish_lifecycle(
                TurnFailed(
                    event_id=_new_event_id(),
                    trace_id=trace_id,
                    session_key=msg.session_key,
                    turn_id=None,
                    message_id=msg.message_id,
                    error=str(exc)[:500],
                ),
            )

    # ── Internal helpers ────────────────────────────────────────────

    async def _build_event_sink(self, msg: InboundMessage) -> AgentEventSink | None:
        """Build a composite sink from configured sinks."""
        if self._event_sink is not None:
            return self._event_sink
        if self._domain_bus is not None:
            return DomainEventBusAgentEventSink(self._domain_bus)
        return None

    async def _publish_lifecycle(self, event: LifecycleEvent) -> None:
        if self._domain_bus is not None:
            try:
                await self._domain_bus.publish(event)
            except Exception:
                logger.exception(
                    "Failed to publish lifecycle event",
                    extra={"event_type": event.event_type},
                )
