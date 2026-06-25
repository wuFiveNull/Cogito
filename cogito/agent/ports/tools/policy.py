# cogito/agent/ports/tools/policy.py
#
# Tool Policy Port — authorisation and risk evaluation for tool calls.
#
# Design rules (see tool-system-spec §13):
#   - Policy evaluates but never executes tools.
#   - DENY results in a synthetic error fed back to the model.
#   - REQUIRE_APPROVAL pauses the entire batch — no tool runs.
#   - Policy is the outermost safety gate before execution.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping, Protocol

from cogito.agent.domain.tools import ToolDefinition, ToolRisk


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
    constraints: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolPolicyRequest:
    """Full context for a policy evaluation."""
    definition: ToolDefinition
    arguments: Mapping[str, object]
    actor_id: str
    session_id: str
    workspace_id: str | None
    channel_capabilities: frozenset[str]
    prior_grants: tuple[object, ...] = ()


class ToolPolicyPort(Protocol):
    """Evaluates whether a tool call is allowed."""

    async def evaluate(
        self,
        request: ToolPolicyRequest,
    ) -> ToolPolicyDecision:
        ...
