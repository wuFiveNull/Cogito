# cogito/agent/runtime/events.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class AgentEventType(StrEnum):
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"

    PHASE_STARTED = "phase_started"
    PHASE_COMPLETED = "phase_completed"
    PHASE_FAILED = "phase_failed"

    RETRIEVAL_STARTED = "retrieval_started"
    RETRIEVAL_COMPLETED = "retrieval_completed"

    MODEL_CALL_STARTED = "model_call_started"
    MODEL_DELTA = "model_delta"
    MODEL_CALL_COMPLETED = "model_call_completed"

    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"

    TURN_SUSPENDED = "turn_suspended"

    KNOWLEDGE_EXTRACTED = "knowledge_extracted"
    PERSISTENCE_COMPLETED = "persistence_completed"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: AgentEventType
    turn_id: str
    request_id: str
    timestamp: datetime
    phase: str | None = None
    data: Mapping[str, object] = field(default_factory=dict)
