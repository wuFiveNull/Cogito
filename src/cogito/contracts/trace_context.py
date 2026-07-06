"""TraceContext — 端到端追踪上下文。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceContext:
    """跨模块因果追踪上下文。"""

    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str | None = None
    correlation_id: str = ""
    causation_id: str = ""
    principal_id: str = ""
    conversation_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    attempt_id: str = ""
    task_id: str = ""

    def __post_init__(self) -> None:
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex
        if not self.span_id:
            self.span_id = uuid.uuid4().hex[:16]

    def new_child(self) -> TraceContext:
        """Create a child span context."""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=self.span_id,
            correlation_id=self.correlation_id or uuid.uuid4().hex[:16],
            causation_id=self.span_id,
            principal_id=self.principal_id,
            conversation_id=self.conversation_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            attempt_id=self.attempt_id,
            task_id=self.task_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "principal_id": self.principal_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceContext:
        return cls(
            trace_id=data.get("trace_id", ""),
            span_id=data.get("span_id", ""),
            parent_span_id=data.get("parent_span_id"),
            correlation_id=data.get("correlation_id", ""),
            causation_id=data.get("causation_id", ""),
            principal_id=data.get("principal_id", ""),
            conversation_id=data.get("conversation_id", ""),
            session_id=data.get("session_id", ""),
            turn_id=data.get("turn_id", ""),
            attempt_id=data.get("attempt_id", ""),
            task_id=data.get("task_id", ""),
        )
