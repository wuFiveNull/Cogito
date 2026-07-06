# cogito/agent/ports/tools/executor.py
#
# Tool Executor Port — executes a single validated, policy-approved call.
#
# Design rules (see tool-system-spec §11.2):
#   - Executor receives a fully prepared and approved call.
#   - It never re-evaluates policy or re-validates arguments.
#   - Returns ToolExecutionResult for business failures;
#     raises only for infrastructure failures that prevent execution.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from cogito.agent.domain.tools import PreparedToolCall, ToolExecutionResult


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Serialisable context for one tool execution.

    This is a stable subset of TurnContext fields (no SDK types,
    no EventSink references, no database connections).
    """

    turn_id: str
    request_id: str
    session_id: str
    actor_id: str
    call_id: str
    idempotency_key: str
    deadline_at: datetime | None
    trace_id: str | None = None
    workspace_id: str | None = None
    workspace_root: str | None = None
    locale: str | None = None
    timezone: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class ToolExecutorPort(Protocol):
    """Executes one policy-approved, validated tool call."""

    async def execute(
        self,
        *,
        prepared_call: PreparedToolCall,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        ...
