# cogito/agent/ports/tools.py
#
# Tool-related ports for the Agent runtime.
#
# Design rules (see agent-loop-phase-spec §7.3, §7.5):
#   - ToolRegistryPort: resolve tool names and validate arguments.
#   - ToolExecutorPort: execute a single prepared tool call.
#   - ToolExecutorPort does NOT receive TurnContext.
#   - Executor returns ToolExecutionResult for business failures,
#     raises for infrastructure failures.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol

from cogito.agent.domain.tools import (
    PreparedToolCall,
    ToolDefinition,
    ToolExecutionResult,
)


class ToolRegistryPort(Protocol):
    """Resolves tool names and validates arguments for AgentLoopPhase.

    ``resolve`` returns the matching ToolDefinition or None.
    ``validate_arguments`` raises on invalid arguments (Schema viol.).
    """

    def resolve(
        self,
        *,
        name: str,
        available_tools: tuple[ToolDefinition, ...],
    ) -> ToolDefinition | None:
        ...

    def validate_arguments(
        self,
        *,
        definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> None:
        ...


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Contextual information for a single tool execution.

    This is a stable, serialisable subset of TurnContext fields.
    Executors never receive the full TurnContext.
    """

    turn_id: str
    request_id: str
    session_id: str
    actor_id: str
    call_id: str
    idempotency_key: str
    deadline_at: datetime | None


class ToolExecutorPort(Protocol):
    """Executes one validated, policy-approved tool call."""

    async def execute(
        self,
        *,
        prepared_call: PreparedToolCall,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        ...
