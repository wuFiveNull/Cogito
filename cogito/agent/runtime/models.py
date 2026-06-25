# cogito/agent/runtime/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from cogito.agent.domain.approval import ApprovalDecisionCommand
from cogito.agent.domain.usage import ToolExecutionRecord, UsageSummary


class TurnStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DENIED = "denied"
    WAITING_APPROVAL = "waiting_approval"


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    attachment_id: str
    media_type: str
    name: str | None = None
    uri: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRequest:
    request_id: str
    session_id: str
    actor_id: str
    text: str
    attachments: tuple[AttachmentRef, ...] = ()
    control: ApprovalDecisionCommand | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_id: str
    request_id: str
    session_id: str
    actor_id: str
    status: TurnStatus
    text: str
    usage: UsageSummary
    tool_records: tuple[ToolExecutionRecord, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
