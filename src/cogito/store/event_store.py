"""SQLite append-only EventStore and trace/timeline queries."""

from __future__ import annotations

import json
import sqlite3
from binascii import Error as BinasciiError
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from collections.abc import Iterable, Mapping
from typing import Any

from cogito.contracts.event_query import EventCursorError
from cogito.domain.event import Event, EventClass, EventContext, EventValidationError
from cogito.domain.event_catalog import event_class_for


class StreamVersionConflictError(EventValidationError):
    """The caller appended against a stale aggregate stream version."""


@dataclass(frozen=True, slots=True)
class EventPage:
    """One stable, reverse-chronological Event Explorer page."""

    events: list[Event]
    next_cursor: str | None


class EventStore:
    """Single durable write boundary for immutable runtime facts.

    The store does not commit implicitly; callers can append a business event and
    its causally required follow-up events in one SQLite transaction.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, event: Event, *, expected_version: int | None = None) -> Event:
        """Append one Event through the command-level atomic append primitive."""
        return self.append_many(
            (event,),
            expected_versions={(event.stream_type, event.stream_id): expected_version}
            if expected_version is not None
            else None,
        )[0]

    def append_many(
        self,
        events: Iterable[Event],
        *,
        expected_versions: Mapping[tuple[str, str], int] | None = None,
    ) -> list[Event]:
        """Append causally-related facts atomically.

        A command may change more than one aggregate.  This method validates
        every catalog entry before issuing SQL, then uses a SQLite savepoint so
        a conflict in any stream rolls back every Event from this invocation
        while preserving an enclosing unit-of-work transaction.
        """
        pending = list(events)
        if not pending:
            return []
        for event in pending:
            registered_class = event_class_for(event.event_type)
            if registered_class != event.event_class:
                raise EventValidationError(
                    f"event class mismatch for {event.event_type}: "
                    f"expected {registered_class.value}, got {event.event_class.value}"
                )

        versions: dict[tuple[str, str], int] = {}
        checked: set[tuple[str, str]] = set()
        stored_events: list[Event] = []
        self._conn.execute("SAVEPOINT event_store_append_many")
        try:
            for event in pending:
                if event.idempotency_key:
                    existing = self._find_idempotent(event.producer, event.idempotency_key)
                    if existing is not None:
                        stored_events.append(existing)
                        continue

                stream_key = (event.stream_type, event.stream_id)
                if stream_key not in versions:
                    row = self._conn.execute(
                        "SELECT COALESCE(MAX(stream_version), 0) FROM event_log "
                        "WHERE stream_type=? AND stream_id=?",
                        stream_key,
                    ).fetchone()
                    versions[stream_key] = int(row[0]) if row else 0
                current_version = versions[stream_key]
                if stream_key not in checked:
                    expected = (expected_versions or {}).get(stream_key)
                    if expected is not None and current_version != expected:
                        raise StreamVersionConflictError(
                            f"{event.stream_type}/{event.stream_id} is at {current_version}, "
                            f"expected {expected}"
                        )
                    checked.add(stream_key)

                stored = event.with_stream_version(current_version + 1)
                self._conn.execute(
                    "INSERT INTO event_log ("
                    "event_id,stream_type,stream_id,stream_version,event_type,type_version,"
                    "event_class,producer,occurred_at,trace_id,span_id,parent_span_id,"
                    "correlation_id,causation_id,actor_id,principal_id,conversation_id,"
                    "session_id,turn_id,attempt_id,task_id,summary,attributes_json,payload_ref,"
                    "payload_hash,outcome,error_category,idempotency_key"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        stored.event_id,
                        stored.stream_type,
                        stored.stream_id,
                        stored.stream_version,
                        stored.event_type,
                        stored.type_version,
                        stored.event_class.value,
                        stored.producer,
                        stored.occurred_at,
                        stored.context.trace_id,
                        stored.context.span_id,
                        stored.context.parent_span_id,
                        stored.context.correlation_id,
                        stored.context.causation_id,
                        stored.context.actor_id,
                        stored.context.principal_id,
                        stored.context.conversation_id,
                        stored.context.session_id,
                        stored.context.turn_id,
                        stored.context.attempt_id,
                        stored.context.task_id,
                        stored.summary,
                        json.dumps(stored.attributes, ensure_ascii=False, sort_keys=True),
                        stored.payload_ref,
                        stored.payload_hash,
                        stored.outcome,
                        stored.error_category,
                        stored.idempotency_key,
                    ),
                )
                versions[stream_key] = stored.stream_version
                stored_events.append(stored)
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK TO SAVEPOINT event_store_append_many")
            self._conn.execute("RELEASE SAVEPOINT event_store_append_many")
            raise StreamVersionConflictError("concurrent stream append") from exc
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT event_store_append_many")
            self._conn.execute("RELEASE SAVEPOINT event_store_append_many")
            raise
        self._conn.execute("RELEASE SAVEPOINT event_store_append_many")
        return stored_events

    def read_stream(self, stream_type: str, stream_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM event_log WHERE stream_type=? AND stream_id=? "
            "ORDER BY stream_version ASC",
            (stream_type, stream_id),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def read_stream_type(self, stream_type: str, *, limit: int = 10_000) -> list[Event]:
        """Read all facts for a bounded aggregate family for in-memory replay.

        This is intentionally not a stored projection: callers regroup and
        reduce the immutable Event streams for each request.
        """
        rows = self._conn.execute(
            "SELECT * FROM event_log WHERE stream_type=? "
            "ORDER BY stream_id ASC, stream_version ASC LIMIT ?",
            (stream_type, max(1, min(limit, 50_000))),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def read_events_by_type(
        self, event_types: frozenset[str], *, limit: int = 10_000
    ) -> list[Event]:
        """Read subscribed Event types in causal-friendly chronological order."""
        if not event_types:
            return []
        placeholders = ",".join("?" for _ in event_types)
        rows = self._conn.execute(
            "SELECT * FROM event_log WHERE event_type IN ("
            + placeholders
            + ") ORDER BY occurred_at ASC, event_id ASC LIMIT ?",
            [*sorted(event_types), max(1, min(limit, 50_000))],
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def get(self, event_id: str) -> Event | None:
        row = self._conn.execute(
            "SELECT * FROM event_log WHERE event_id=?", (event_id,)
        ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def find_idempotent(self, producer: str, idempotency_key: str) -> Event | None:
        """Return the fact already accepted for an idempotent command."""
        return self._find_idempotent(producer, idempotency_key)

    def list_events(
        self,
        *,
        limit: int = 100,
        before: int | None = None,
        event_type: str | None = None,
        stream_type: str | None = None,
        stream_id: str | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        attempt_id: str | None = None,
        task_id: str | None = None,
    ) -> list[Event]:
        """Compatibility list wrapper around the cursor-capable Event Explorer."""
        return self.list_events_page(
            limit=limit,
            before=before,
            event_type=event_type,
            stream_type=stream_type,
            stream_id=stream_id,
            trace_id=trace_id,
            session_id=session_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            attempt_id=attempt_id,
            task_id=task_id,
        ).events

    def list_events_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        before: int | None = None,
        event_type: str | None = None,
        stream_type: str | None = None,
        stream_id: str | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        attempt_id: str | None = None,
        task_id: str | None = None,
    ) -> EventPage:
        """List Events with a stable cursor and Catalog-safe filter fields.

        ``before`` is kept only for callers still using the pre-cutover API.
        New clients must use ``cursor`` because timestamps alone cannot safely
        order Events that occurred in the same millisecond.
        """
        clauses = ["1=1"]
        params: list[Any] = []
        cursor_time, cursor_id = self._decode_cursor(cursor) if cursor else (None, None)
        if cursor_time is not None:
            clauses.append("(occurred_at < ? OR (occurred_at = ? AND event_id < ?))")
            params.extend((cursor_time, cursor_time, cursor_id))
        if before is not None:
            clauses.append("occurred_at < ?")
            params.append(before)
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if stream_type:
            clauses.append("stream_type=?")
            params.append(stream_type)
        if stream_id:
            clauses.append("stream_id=?")
            params.append(stream_id)
        if trace_id:
            clauses.append("trace_id=?")
            params.append(trace_id)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        for column, value in (
            ("correlation_id", correlation_id),
            ("causation_id", causation_id),
            ("conversation_id", conversation_id),
            ("turn_id", turn_id),
            ("attempt_id", attempt_id),
            ("task_id", task_id),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        page_size = max(1, min(limit, 500))
        params.append(page_size + 1)
        rows = self._conn.execute(
            "SELECT * FROM event_log WHERE " + " AND ".join(clauses) + " "
            "ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
            params,
        ).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        events = [self._row_to_event(row) for row in rows]
        next_cursor = None
        if has_more and events:
            last = events[-1]
            next_cursor = self._encode_cursor(last.occurred_at, last.event_id)
        return EventPage(events=events, next_cursor=next_cursor)

    def trace(self, trace_id: str) -> dict[str, Any] | None:
        rows = self._conn.execute(
            "SELECT * FROM event_log WHERE trace_id=? ORDER BY occurred_at ASC,rowid ASC",
            (trace_id,),
        ).fetchall()
        if not rows:
            return None
        events = [self._row_to_event(row) for row in rows]
        by_span = {
            event.context.span_id: event.event_id
            for event in events
            if event.context.span_id
        }
        return {
            "trace_id": trace_id,
            "events": [event.to_dict() for event in events],
            "edges": [
                {
                    "event_id": event.event_id,
                    "parent_event_id": by_span.get(event.context.parent_span_id or ""),
                    "causation_id": event.context.causation_id,
                }
                for event in events
            ],
        }

    def _find_idempotent(self, producer: str, idempotency_key: str) -> Event | None:
        row = self._conn.execute(
            "SELECT * FROM event_log WHERE producer=? AND idempotency_key=?",
            (producer, idempotency_key),
        ).fetchone()
        return self._row_to_event(row) if row is not None else None

    @staticmethod
    def _encode_cursor(occurred_at: int, event_id: str) -> str:
        raw = json.dumps([occurred_at, event_id], separators=(",", ":")).encode("utf-8")
        return urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[int, str]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            occurred_at, event_id = json.loads(urlsafe_b64decode(padded).decode("utf-8"))
            if not isinstance(occurred_at, int) or not isinstance(event_id, str) or not event_id:
                raise ValueError("invalid cursor fields")
            return occurred_at, event_id
        except (BinasciiError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise EventCursorError("invalid Event Explorer cursor") from exc

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            stream_type=row["stream_type"],
            stream_id=row["stream_id"],
            stream_version=int(row["stream_version"]),
            event_type=row["event_type"],
            type_version=int(row["type_version"]),
            event_class=EventClass(row["event_class"]),
            producer=row["producer"],
            occurred_at=int(row["occurred_at"]),
            context=EventContext(
                trace_id=row["trace_id"],
                span_id=row["span_id"],
                parent_span_id=row["parent_span_id"],
                correlation_id=row["correlation_id"],
                causation_id=row["causation_id"],
                actor_id=row["actor_id"],
                principal_id=row["principal_id"],
                conversation_id=row["conversation_id"],
                session_id=row["session_id"],
                turn_id=row["turn_id"],
                attempt_id=row["attempt_id"],
                task_id=row["task_id"],
            ),
            summary=row["summary"],
            attributes=json.loads(row["attributes_json"]),
            payload_ref=row["payload_ref"],
            payload_hash=row["payload_hash"],
            outcome=row["outcome"],
            error_category=row["error_category"],
            idempotency_key=row["idempotency_key"],
        )
