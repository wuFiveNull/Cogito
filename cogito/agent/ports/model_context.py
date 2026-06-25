# cogito/agent/ports/model_context.py
#
# ModelContextWindowPort — dynamic context window fitting for AgentLoopPhase.
#
# Design rules (see agent-loop-phase-spec §7.2):
#   - Called before each model invocation to trim the message list
#     to fit the model's context window.
#   - Never modifies ctx.model_messages (canonical list is read-only).
#   - Returns a fitted (possibly compressed) tuple for that call's view.
#   - May compress large tool results, but never drops the current
#     user input, core system rules, or unmatched tool calls.
#   - Raises ContextWindowExceededError when fitting is impossible.

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cogito.agent.domain.messages import ModelMessage
from cogito.agent.domain.tools import ToolDefinition


@dataclass(frozen=True, slots=True)
class ContextWindowRequest:
    """Input for a context window fit operation."""

    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    reserved_output_tokens: int


class ModelContextWindowPort(Protocol):
    """Dynamically fits a message list into the model's context window."""

    async def fit(
        self,
        request: ContextWindowRequest,
    ) -> tuple[ModelMessage, ...]:
        ...
