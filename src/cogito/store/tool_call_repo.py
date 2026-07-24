"""Tool Call Repository — tool_calls 表持久化。

利用已存在的 tool_calls 数据库表（见 schema.py）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_replay import ToolCallProjection, replay_tool_call
from cogito.store.event_store import EventStore


@dataclass
class ToolCallRecord:
    """tool_calls 表的值对象。"""

    tool_call_id: str
    attempt_id: str
    attempt_type: str = "run"
    tool_name: str = ""
    tool_version: str = "1.0"
    arguments: str = "{}"
    arguments_ref: str = ""
    idempotency_key: str = ""
    status: Literal[
        "pending", "approved", "executing", "succeeded", "failed", "unknown", "cancelled"
    ] = "pending"
    started_at: int | None = None
    completed_at: int | None = None
    result_ref: str = ""
    result_summary: str = ""
    result_trust_label: str = "unverified"
    result_size_bytes: int = 0
    constraints_json: str = "{}"


class ToolCallRepository:
    """ToolCall Event read/write boundary; tool-call rows are not persisted."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ToolCallRecord) -> None:
        """Append an immutable tool lifecycle without storing arguments/result text."""
        context = self._event_context_for_attempt(record.attempt_id)
        requested = EventStore(self._conn).append(
            Event(
                event_type="tool.call.requested",
                stream_type="tool_call",
                stream_id=record.tool_call_id,
                producer="tool-call-repository",
                event_class=EventClass.OPERATION,
                context=context,
                summary=f"Tool requested: {record.tool_name}"[:2_000],
                attributes={
                    "tool_name": record.tool_name,
                    "tool_version": record.tool_version,
                    "attempt_type": record.attempt_type,
                },
                payload_ref=record.arguments_ref or None,
                outcome="pending",
                occurred_at=record.started_at or 0,
                idempotency_key=f"tool-call:{record.tool_call_id}:requested",
            )
        )
        if record.status != "pending":
            self._append_status_event(
                record,
                record.status,
                context=self._child_context(context, requested.event_id),
                occurred_at=record.completed_at or record.started_at,
                result_ref=record.result_ref,
                result_trust_label=record.result_trust_label,
                result_size_bytes=record.result_size_bytes,
            )

    def update_status(
        self,
        tool_call_id: str,
        status: str,
        completed_at: int | None = None,
        *,
        result_ref: str = "",
        result_summary: str = "",
        result_trust_label: str = "unverified",
        result_size_bytes: int = 0,
    ) -> None:
        """Append the next immutable ToolCall lifecycle fact."""
        existing = self.find(tool_call_id)
        if existing is None:
            return
        prior = EventStore(self._conn).read_stream("tool_call", tool_call_id)
        source = prior[-1].context if prior else self._event_context_for_attempt(existing.attempt_id)
        causation_id = prior[-1].event_id if prior else source.causation_id
        self._append_status_event(
            existing,
            status,
            context=self._child_context(source, causation_id),
            occurred_at=completed_at or existing.started_at,
            result_ref=result_ref,
            result_trust_label=result_trust_label,
            result_size_bytes=result_size_bytes,
        )

    def _event_context_for_attempt(self, attempt_id: str) -> EventContext:
        events = EventStore(self._conn).list_events(attempt_id=attempt_id, limit=500)
        source = next((event for event in events if event.context.trace_id), None)
        if source is None:
            return EventContext(attempt_id=attempt_id)
        return self._child_context(source.context, source.event_id, attempt_id=attempt_id)

    def find_by_attempt(self, attempt_id: str) -> list[ToolCallRecord]:
        """Rebuild all ToolCalls for an Attempt from canonical Events."""
        return self._replayed_records(
            event for event in self._events() if event.context.attempt_id == attempt_id
        )

    def find(self, tool_call_id: str) -> ToolCallRecord | None:
        """Rebuild a ToolCall by its Event stream id."""
        projection = replay_tool_call(self._events(), tool_call_id)
        return self._projection_to_record(projection) if projection is not None else None

    @staticmethod
    def _child_context(
        source: EventContext, causation_id: str, *, attempt_id: str | None = None
    ) -> EventContext:
        return EventContext(
            trace_id=source.trace_id,
            span_id=source.span_id,
            parent_span_id=source.parent_span_id,
            correlation_id=source.correlation_id,
            causation_id=causation_id,
            actor_id=source.actor_id,
            principal_id=source.principal_id,
            conversation_id=source.conversation_id,
            session_id=source.session_id,
            turn_id=source.turn_id,
            attempt_id=attempt_id or source.attempt_id,
            task_id=source.task_id,
        )

    @staticmethod
    def _event_type_for_status(status: str) -> str | None:
        return {
            "approved": "tool.call.approval_required",
            "executing": "tool.call.started",
            "succeeded": "tool.call.completed",
            "failed": "tool.call.failed",
            "cancelled": "tool.call.cancelled",
            "unknown": "tool.call.unknown",
        }.get(status)

    def _append_status_event(
        self,
        record: ToolCallRecord,
        status: str,
        *,
        context: EventContext,
        occurred_at: int | None,
        result_ref: str,
        result_trust_label: str,
        result_size_bytes: int,
    ) -> None:
        event_type = self._event_type_for_status(status)
        if event_type is None:
            return
        EventStore(self._conn).append(
            Event(
                event_type=event_type,
                stream_type="tool_call",
                stream_id=record.tool_call_id,
                producer="tool-call-repository",
                event_class=EventClass.OPERATION,
                context=context,
                summary=f"Tool {status}: {record.tool_name}"[:2_000],
                attributes={
                    "tool_name": record.tool_name,
                    "tool_version": record.tool_version,
                    "attempt_type": record.attempt_type,
                    "result_size_bytes": result_size_bytes,
                    "result_trust_label": result_trust_label,
                },
                payload_ref=result_ref or None,
                outcome=status,
                occurred_at=occurred_at or 0,
                idempotency_key=f"tool-call:{record.tool_call_id}:{status}:{occurred_at or ''}",
            )
        )

    def list_recent(self, limit: int = 50) -> list[ToolCallRecord]:
        return sorted(
            self._replayed_records(self._events()),
            key=lambda record: record.started_at or 0,
            reverse=True,
        )[:limit]

    def _events(self):
        return EventStore(self._conn).read_stream_type("tool_call")

    @staticmethod
    def _replayed_records(events) -> list[ToolCallRecord]:
        grouped: dict[str, list[Event]] = {}
        for event in events:
            grouped.setdefault(event.stream_id, []).append(event)
        records = [
            ToolCallRepository._projection_to_record(projection)
            for tool_call_id, stream in grouped.items()
            if (projection := replay_tool_call(stream, tool_call_id)) is not None
        ]
        return sorted(records, key=lambda record: record.started_at or 0)

    @staticmethod
    def _projection_to_record(projection: ToolCallProjection) -> ToolCallRecord:
        return ToolCallRecord(
            tool_call_id=projection.tool_call_id,
            attempt_id=projection.attempt_id,
            attempt_type=projection.attempt_type,
            tool_name=projection.tool_name,
            tool_version=projection.tool_version,
            arguments_ref=projection.arguments_ref,
            status=projection.status,
            started_at=projection.started_at,
            completed_at=projection.completed_at,
            result_ref=projection.result_ref,
            result_trust_label=projection.result_trust_label,
            result_size_bytes=projection.result_size_bytes,
        )
