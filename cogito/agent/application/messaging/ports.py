# cogito/agent/application/messaging/ports.py

from __future__ import annotations

from typing import Protocol

from cogito.agent.application.messaging.envelope import MessageEnvelope


class MessagePublisherPort(Protocol):
    """Publishes a MessageEnvelope to a named destination."""

    async def publish(
        self,
        *,
        destination: str,
        envelope: MessageEnvelope,
    ) -> None:
        ...
