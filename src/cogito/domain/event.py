"""Canonical immutable Event contract for the event-sourced runtime.

The payload deliberately contains only a safe summary and a payload reference.
Raw prompts, tool arguments, model responses, and secrets belong in the guarded
payload store, never in the append-only event log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from time import time
from typing import Any


class EventClass(StrEnum):
    DOMAIN = "domain"
    OPERATION = "operation"
    TELEMETRY = "telemetry"


class EventValidationError(ValueError):
    """Raised when an event would put unsafe or invalid data in the log."""


@dataclass(frozen=True, slots=True)
class EventContext:
    """Causal and subject identifiers propagated across the complete lifecycle."""

    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str | None = None
    correlation_id: str = ""
    causation_id: str = ""
    actor_id: str = ""
    principal_id: str = ""
    conversation_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    attempt_id: str = ""
    task_id: str = ""

    def child(self, *, span_id: str | None = None) -> EventContext:
        """Return a child context without changing cross-event correlation."""
        return replace(
            self,
            span_id=span_id or uuid.uuid4().hex[:16],
            parent_span_id=self.span_id or None,
            causation_id=self.span_id or self.causation_id,
        )


_FORBIDDEN_ATTRIBUTE_KEYS = frozenset(
    {
        "content",
        "prompt",
        "response",
        "arguments",
        "raw_payload",
        "secret",
        "api_key",
        "authorization",
    }
)


def _unsafe_attribute_paths(value: Any, *, path: str = "attributes") -> list[str]:
    """Find sensitive keys at every level of a JSON-shaped Event attribute.

    A shallow key check can be bypassed with ``{"metadata": {"prompt": ...}}``.
    Attributes are deliberately small, safe observability fields; detailed
    requests and responses belong to the guarded payload store.
    """
    if isinstance(value, dict):
        paths: list[str] = []
        for key, nested in value.items():
            key_text = str(key)
            nested_path = f"{path}.{key_text}"
            if key_text.lower() in _FORBIDDEN_ATTRIBUTE_KEYS:
                paths.append(nested_path)
            paths.extend(_unsafe_attribute_paths(nested, path=nested_path))
        return paths
    if isinstance(value, (list, tuple)):
        paths = []
        for index, nested in enumerate(value):
            paths.extend(_unsafe_attribute_paths(nested, path=f"{path}[{index}]"))
        return paths
    return []


@dataclass(frozen=True, slots=True)
class Event:
    """The one durable fact shape used by the runtime.

    ``stream_version`` is allocated by :class:`EventStore`; producers leave it
    at zero. ``idempotency_key`` is intentionally an envelope property rather
    than event content, and is never shown in a trace timeline.
    """

    event_type: str
    stream_type: str
    stream_id: str
    producer: str
    event_class: EventClass
    context: EventContext = field(default_factory=EventContext)
    summary: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    payload_ref: str | None = None
    payload_hash: str = ""
    outcome: str = ""
    error_category: str = ""
    type_version: int = 1
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stream_version: int = 0
    occurred_at: int = field(default_factory=lambda: int(time() * 1000))
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if not self.event_type or not self.stream_type or not self.stream_id or not self.producer:
            raise EventValidationError("event_type, stream_type, stream_id, and producer are required")
        if self.type_version < 1:
            raise EventValidationError("type_version must be positive")
        if self.stream_version < 0:
            raise EventValidationError("stream_version cannot be negative")
        if self.occurred_at <= 0:
            object.__setattr__(self, "occurred_at", int(time() * 1000))
        if len(self.summary) > 2_000:
            raise EventValidationError("summary exceeds 2000 characters")
        forbidden = _unsafe_attribute_paths(self.attributes)
        if forbidden:
            raise EventValidationError(f"unsafe event attributes: {', '.join(sorted(forbidden))}")

    def with_stream_version(self, version: int) -> Event:
        if version < 1:
            raise EventValidationError("stream_version must be positive once appended")
        return replace(self, stream_version=version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "stream_type": self.stream_type,
            "stream_id": self.stream_id,
            "stream_version": self.stream_version,
            "event_type": self.event_type,
            "type_version": self.type_version,
            "event_class": self.event_class.value,
            "producer": self.producer,
            "occurred_at": self.occurred_at,
            "trace_id": self.context.trace_id,
            "span_id": self.context.span_id,
            "parent_span_id": self.context.parent_span_id,
            "correlation_id": self.context.correlation_id,
            "causation_id": self.context.causation_id,
            "actor_id": self.context.actor_id,
            "principal_id": self.context.principal_id,
            "conversation_id": self.context.conversation_id,
            "session_id": self.context.session_id,
            "turn_id": self.context.turn_id,
            "attempt_id": self.context.attempt_id,
            "task_id": self.context.task_id,
            "summary": self.summary,
            "attributes": self.attributes,
            "payload_ref": self.payload_ref,
            "payload_hash": self.payload_hash,
            "outcome": self.outcome,
            "error_category": self.error_category,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        """Rehydrate the one durable Event contract from its stored shape."""
        return cls(
            event_id=str(data.get("event_id", "")) or uuid.uuid4().hex,
            event_type=str(data["event_type"]),
            stream_type=str(data["stream_type"]),
            stream_id=str(data["stream_id"]),
            stream_version=int(data.get("stream_version", 0)),
            type_version=int(data.get("type_version", 1)),
            event_class=EventClass(data["event_class"]),
            producer=str(data["producer"]),
            occurred_at=int(data.get("occurred_at", 0)),
            context=EventContext(
                trace_id=str(data.get("trace_id", "")),
                span_id=str(data.get("span_id", "")),
                parent_span_id=data.get("parent_span_id"),
                correlation_id=str(data.get("correlation_id", "")),
                causation_id=str(data.get("causation_id", "")),
                actor_id=str(data.get("actor_id", "")),
                principal_id=str(data.get("principal_id", "")),
                conversation_id=str(data.get("conversation_id", "")),
                session_id=str(data.get("session_id", "")),
                turn_id=str(data.get("turn_id", "")),
                attempt_id=str(data.get("attempt_id", "")),
                task_id=str(data.get("task_id", "")),
            ),
            summary=str(data.get("summary", "")),
            attributes=dict(data.get("attributes") or {}),
            payload_ref=data.get("payload_ref"),
            payload_hash=str(data.get("payload_hash", "")),
            outcome=str(data.get("outcome", "")),
            error_category=str(data.get("error_category", "")),
            idempotency_key=str(data.get("idempotency_key", "")),
        )
