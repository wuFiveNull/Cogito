# cogito/agent/application/messaging/worker.py

from __future__ import annotations

import logging

from cogito.agent.application.agent_service import AgentApplicationService
from cogito.agent.application.messaging.envelope import MessageEnvelope
from cogito.agent.application.messaging.mapper import (
    AgentOutputMapper,
    AgentRequestMapper,
)
from cogito.agent.application.messaging.ports import MessagePublisherPort
from cogito.agent.application.messaging.sink import BusAgentEventSink
from cogito.agent.runtime.errors import RuntimeAgentError

logger = logging.getLogger(__name__)


class AgentMessageWorker:
    """Bridges MessageBus messages to the Application Service.

    This worker:
    - Converts MessageEnvelope → AgentRequest via mapper.
    - Calls AgentApplicationService.process().
    - Publishes result/error back via MessagePublisherPort.

    It is aware of both MessageBus abstractions and the Application Service.
    The Kernel knows neither.
    """

    def __init__(
        self,
        *,
        service: AgentApplicationService,
        request_mapper: AgentRequestMapper,
        output_mapper: AgentOutputMapper,
        publisher: MessagePublisherPort,
    ) -> None:
        self._service = service
        self._request_mapper = request_mapper
        self._output_mapper = output_mapper
        self._publisher = publisher

    async def handle(
        self,
        envelope: MessageEnvelope,
    ) -> None:
        request = self._request_mapper.to_request(envelope)

        sink = BusAgentEventSink(
            source=envelope,
            mapper=self._output_mapper,
            publisher=self._publisher,
        )

        try:
            result = await self._service.process(
                request,
                event_sink=sink,
            )
        except RuntimeAgentError as exc:
            error_envelope = self._output_mapper.error_to_envelope(
                source=envelope,
                error=exc,
            )
            await self._publisher.publish(
                destination=envelope.reply_to or "agent.output",
                envelope=error_envelope,
            )
            return

        result_envelope = self._output_mapper.result_to_envelope(
            source=envelope,
            result=result,
        )

        await self._publisher.publish(
            destination=envelope.reply_to or "agent.output",
            envelope=result_envelope,
        )
