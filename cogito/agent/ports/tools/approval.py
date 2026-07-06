# cogito/agent/ports/tools/approval.py
#
# Tool Approval Port — user consent for high-risk tool calls.
#
# Design rules (see tool-system-spec §14):
#   - Approval always happens BEFORE tool execution.
#   - Two modes: interactive (local CLI wait) and durable (MessageBus suspend).
#   - Approval tickets are bound to actor, turn, call — never global.
#   - Checkpoints enable safe resume after approval.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class ToolApprovalCallInfo:
    call_id: str
    tool_name: str
    risk_summary: str
    argument_summary: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolApprovalRequest:
    approval_id: str
    turn_id: str
    actor_id: str
    session_id: str
    calls: tuple[ToolApprovalCallInfo, ...]
    requested_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ToolApprovalResult:
    approval_id: str
    status: str  # "approved" | "rejected" | "expired" | "pending"
    decision: str | None = None  # "allow" | "deny"


class ToolApprovalCoordinatorPort(Protocol):
    """Coordinates the approval lifecycle for tool calls."""

    async def request_approval(
        self,
        request: ToolApprovalRequest,
    ) -> ToolApprovalResult:
        ...

    async def resolve_approval(
        self,
        approval_id: str,
        decision: str,
    ) -> None:
        ...
