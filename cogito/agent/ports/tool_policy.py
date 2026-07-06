# cogito/agent/ports/tool_policy.py
#
# ToolPolicyPort — authorisation and risk evaluation for tool calls.
#
# Design rules (see agent-loop-phase-spec §7.4):
#   - AgentLoop never hard-codes permissions by tool name.
#   - Policy evaluates but never executes tools.
#   - DENY results in a synthetic error fed back to the model.
#   - REQUIRE_APPROVAL pauses the entire batch — no tool runs.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from cogito.agent.domain.tools import PreparedToolCall


class ToolPolicyDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    decision: ToolPolicyDecisionType
    reason_code: str
    safe_message: str
    approval_prompt: str | None = None


class ToolPolicyPort(Protocol):
    """Evaluates whether a prepared tool call is allowed."""

    async def evaluate(
        self,
        *,
        actor_id: str,
        session_id: str,
        prepared_call: PreparedToolCall,
    ) -> ToolPolicyDecision:
        ...
