# cogito/agent/application/messaging/sink.py

"""BusAgentEventSink — bridges runtime AgentEvents to the MessageBus."""

from __future__ import annotations

import logging

from cogito.agent.application.messaging.envelope import MessageEnvelope
from cogito.agent.application.messaging.mapper import AgentOutputMapper
from cogito.agent.application.messaging.ports import MessagePublisherPort
from cogito.agent.ports.events import AgentEventSink
from cogito.agent.runtime.events import AgentEvent

logger = logging.getLogger(__name__)


class BusAgentEventSink:
    """An AgentEventSink that publishes events to the MessageBus.

    Each emitted event is mapped to a MessageEnvelope via the output mapper
    and published to the appropriate destination derived from the source envelope.
    """

    def __init__(
        self,
        *,
        source: MessageEnvelope,
        mapper: AgentOutputMapper,
        publisher: MessagePublisherPort,
    ) -> None:
        self._source = source
        self._mapper = mapper
        self._publisher = publisher

    async def emit(self, event: AgentEvent) -> None:
        try:
            envelope = self._mapper.event_to_envelope(
                source=self._source,
                event=event,
            )
            await self._publisher.publish(
                destination=self._source.reply_to or "agent.events",
                envelope=envelope,
            )
        except Exception:
            logger.exception(
                "Failed to publish agent event to bus",
                extra={"event_type": event.type},
            )
