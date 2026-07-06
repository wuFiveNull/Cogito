# cogito/agent/runtime/context_factory.py — TurnContextFactory

from __future__ import annotations

from dataclasses import dataclass

from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.ids import IdGeneratorPort
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.models import AgentRequest, TurnStatus


@dataclass(slots=True)
class TurnContextFactory:
    """Pre-initializes turn identity before any events are emitted.

    Responsibilities (limited to minimum identity info):
      - Generate turn_id.
      - Record started_at.
      - Set status to RUNNING.

    Factory explicitly does NOT:
      - Start trace.
      - Validate business request.
      - Load any Repository.
      - Configure tool rounds.
      - Send events.
      - Contain Channel or MessageBus types.
    """

    clock: ClockPort
    id_generator: IdGeneratorPort

    def create(self, request: AgentRequest) -> TurnContext:
        return TurnContext(
            request=request,
            turn_id=self.id_generator.new_id(),
            started_at=self.clock.now(),
            status=TurnStatus.RUNNING,
        )
