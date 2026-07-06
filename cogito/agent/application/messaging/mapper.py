# cogito/agent/application/messaging/mapper.py

from __future__ import annotations

from typing import Protocol

from cogito.agent.application.messaging.envelope import MessageEnvelope
from cogito.agent.runtime.events import AgentEvent
from cogito.agent.runtime.models import AgentRequest, TurnResult


class AgentRequestMapper(Protocol):
    """Maps inbound MessageEnvelope to AgentRequest."""

    def to_request(
        self,
        envelope: MessageEnvelope,
    ) -> AgentRequest:
        ...


class AgentOutputMapper(Protocol):
    """Maps runtime output back to MessageEnvelope."""

    def event_to_envelope(
        self,
        *,
        source: MessageEnvelope,
        event: AgentEvent,
    ) -> MessageEnvelope:
        ...

    def result_to_envelope(
        self,
        *,
        source: MessageEnvelope,
        result: TurnResult,
    ) -> MessageEnvelope:
        ...

    def error_to_envelope(
        self,
        *,
        source: MessageEnvelope,
        error: object,
    ) -> MessageEnvelope:
        ...
