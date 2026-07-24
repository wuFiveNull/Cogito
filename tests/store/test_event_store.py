"""Canonical EventStore invariants."""

from __future__ import annotations

import pytest

from cogito.contracts.envelope import ChannelEnvelope
from cogito.domain.event import Event, EventClass, EventContext, EventValidationError
from cogito.service.dispatcher import Dispatcher
from cogito.service.inbound_service import InboundService
from cogito.store.event_store import EventStore, StreamVersionConflictError
from cogito.store.legacy_event_backfill import LegacyEventBackfill
from cogito.store.tool_call_repo import ToolCallRecord, ToolCallRepository


def _event(*, key: str = "", context: EventContext | None = None) -> Event:
    return Event(
        event_type="runtime.turn.queued",
        stream_type="turn",
        stream_id="turn-1",
        producer="test",
        event_class=EventClass.DOMAIN,
        context=context or EventContext(trace_id="trace-1", span_id="span-1", turn_id="turn-1"),
        summary="Turn queued",
        attributes={"priority": 80},
        idempotency_key=key,
    )


def test_append_allocates_monotonic_stream_versions(in_memory_db):
    store = EventStore(in_memory_db)
    first = store.append(_event())
    second = store.append(
        Event(
            event_type="runtime.turn.started",
            stream_type="turn",
            stream_id="turn-1",
            producer="test",
            event_class=EventClass.OPERATION,
        )
    )
    assert (first.stream_version, second.stream_version) == (1, 2)
    assert [event.event_type for event in store.read_stream("turn", "turn-1")] == [
        "runtime.turn.queued",
        "runtime.turn.started",
    ]


def test_append_detects_stale_expected_version(in_memory_db):
    store = EventStore(in_memory_db)
    store.append(_event(), expected_version=0)
    with pytest.raises(StreamVersionConflictError):
        store.append(_event(), expected_version=0)


def test_idempotent_append_returns_original_event(in_memory_db):
    store = EventStore(in_memory_db)
    first = store.append(_event(key="request-1"))
    duplicate = store.append(_event(key="request-1"))
    assert duplicate.event_id == first.event_id
    assert len(store.read_stream("turn", "turn-1")) == 1


def test_append_many_is_atomic_across_streams(in_memory_db):
    store = EventStore(in_memory_db)
    first = _event()
    conflicting = Event(
        event_type="task.created",
        stream_type="task",
        stream_id="task-1",
        producer="test",
        event_class=EventClass.DOMAIN,
    )

    with pytest.raises(StreamVersionConflictError):
        store.append_many(
            (first, conflicting),
            expected_versions={("turn", "turn-1"): 0, ("task", "task-1"): 1},
        )

    assert store.read_stream("turn", "turn-1") == []
    assert store.read_stream("task", "task-1") == []


def test_append_many_allocates_versions_within_one_stream(in_memory_db):
    store = EventStore(in_memory_db)
    queued, started = store.append_many(
        (
            _event(),
            Event(
                event_type="runtime.turn.started",
                stream_type="turn",
                stream_id="turn-1",
                producer="test",
                event_class=EventClass.OPERATION,
            ),
        ),
        expected_versions={("turn", "turn-1"): 0},
    )
    assert (queued.stream_version, started.stream_version) == (1, 2)


def test_catalog_and_sensitive_attributes_are_enforced(in_memory_db):
    store = EventStore(in_memory_db)
    with pytest.raises(EventValidationError, match="unregistered"):
        store.append(
            Event(
                event_type="runtime.turn.do_thing",
                stream_type="turn",
                stream_id="x",
                producer="test",
                event_class=EventClass.DOMAIN,
            )
        )
    with pytest.raises(EventValidationError, match="unsafe"):
        Event(
            event_type="runtime.turn.queued",
            stream_type="turn",
            stream_id="x",
            producer="test",
            event_class=EventClass.DOMAIN,
            attributes={"prompt": "must not be here"},
        )
    with pytest.raises(EventValidationError, match="metadata.prompt"):
        Event(
            event_type="runtime.turn.queued",
            stream_type="turn",
            stream_id="x",
            producer="test",
            event_class=EventClass.DOMAIN,
            attributes={"metadata": {"prompt": "must not be nested here"}},
        )


def test_trace_returns_causal_edges(in_memory_db):
    store = EventStore(in_memory_db)
    root = store.append(_event(context=EventContext(trace_id="trace-1", span_id="root")))
    child = store.append(
        Event(
            event_type="runtime.turn.started",
            stream_type="turn",
            stream_id="turn-1",
            producer="test",
            event_class=EventClass.OPERATION,
            context=EventContext(trace_id="trace-1", span_id="child", parent_span_id="root"),
        )
    )
    trace = store.trace("trace-1")
    assert trace is not None
    assert [item["event_id"] for item in trace["events"]] == [root.event_id, child.event_id]
    assert trace["edges"][1]["parent_event_id"] == root.event_id


def test_event_explorer_cursor_is_stable_and_filters_causal_subjects(in_memory_db):
    store = EventStore(in_memory_db)
    first = store.append(
        Event(
            event_id="event-c",
            event_type="runtime.turn.queued",
            stream_type="turn",
            stream_id="turn-c",
            producer="test",
            event_class=EventClass.DOMAIN,
            occurred_at=100,
            context=EventContext(correlation_id="request-1", turn_id="turn-c"),
        )
    )
    second = store.append(
        Event(
            event_id="event-b",
            event_type="runtime.turn.queued",
            stream_type="turn",
            stream_id="turn-b",
            producer="test",
            event_class=EventClass.DOMAIN,
            occurred_at=100,
            context=EventContext(correlation_id="request-1", turn_id="turn-b"),
        )
    )
    third = store.append(
        Event(
            event_id="event-a",
            event_type="runtime.turn.queued",
            stream_type="turn",
            stream_id="turn-a",
            producer="test",
            event_class=EventClass.DOMAIN,
            occurred_at=99,
            context=EventContext(correlation_id="request-2", turn_id="turn-a"),
        )
    )

    page_one = store.list_events_page(limit=2, correlation_id="request-1")
    assert [event.event_id for event in page_one.events] == [first.event_id, second.event_id]
    assert page_one.next_cursor is None

    all_first_page = store.list_events_page(limit=2)
    assert [event.event_id for event in all_first_page.events] == [first.event_id, second.event_id]
    assert all_first_page.next_cursor
    all_second_page = store.list_events_page(limit=2, cursor=all_first_page.next_cursor)
    assert [event.event_id for event in all_second_page.events] == [third.event_id]


def test_legacy_backfill_is_safe_and_idempotent(in_memory_db):
    in_memory_db.execute(
        "INSERT INTO tasks (task_id,task_type,status,priority,idempotency_key,origin,created_at) "
        "VALUES ('legacy-task','maintenance','completed',1,'legacy-key','test',1000)"
    )
    backfill = LegacyEventBackfill(in_memory_db)
    assert backfill.import_table("tasks", "task_id", "task") == 1
    assert backfill.import_table("tasks", "task_id", "task") == 0
    event = EventStore(in_memory_db).read_stream("legacy", "tasks:legacy-task")[0]
    assert event.event_type == "legacy.task.imported"
    assert event.attributes == {"entity_type": "task", "legacy_table": "tasks", "status": "completed"}


def test_legacy_drift_backfill_creates_replayable_snapshot(in_memory_db):
    from cogito.store.drift_repo import DriftRunRepository

    in_memory_db.execute(
        "INSERT INTO tasks (task_id,task_type,status,priority,idempotency_key,origin,created_at) "
        "VALUES ('legacy-drift-task','drift.run','completed',1,'legacy-drift-task','test',1000)"
    )
    in_memory_db.execute(
        "INSERT INTO drift_runs (drift_run_id,task_id,principal_id,skill_name,skill_version,"
        "status,steps_taken,preemption_reason,admission_snapshot_json,created_at) VALUES "
        "('legacy-drift','legacy-drift-task','owner','policy-audit','1.0','paused',3,'active_turn','{}',2000)"
    )

    LegacyEventBackfill(in_memory_db).import_table("drift_runs", "drift_run_id", "drift_run")

    run = DriftRunRepository(in_memory_db, event_sourced=True).get("legacy-drift")
    assert run is not None
    assert run["status"] == "paused"
    assert run["skill_name"] == "policy-audit"
    assert run["steps_taken"] == 3
    assert run["preemption_reason"] == "active_turn"


def test_tool_lifecycle_inherits_attempt_event_context(in_memory_db):
    accepted = InboundService(in_memory_db).accept(
        ChannelEnvelope(
            channel_type="test",
            channel_instance_id="tool-context",
            platform_sender_id="principal-1",
            platform_conversation_id="conversation-1",
            platform_message_id="message-1",
            content_parts=[{"content_type": "text", "inline_data": "run a tool"}],
        )
    )
    claimed = Dispatcher(in_memory_db).claim_next("worker-1")
    assert claimed is not None

    repository = ToolCallRepository(in_memory_db)
    repository.insert(
        ToolCallRecord(
            tool_call_id="tool-call-1",
            attempt_id=claimed.attempt.attempt_id,
            tool_name="lookup",
            status="executing",
            started_at=1,
        )
    )
    repository.update_status("tool-call-1", "succeeded", completed_at=2)

    events = EventStore(in_memory_db).read_stream("tool_call", "tool-call-1")
    assert [event.event_type for event in events] == [
        "tool.call.requested",
        "tool.call.started",
        "tool.call.completed",
    ]
    assert in_memory_db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    assert all(event.context.trace_id for event in events)
    assert all(event.context.turn_id == accepted.turn_id for event in events)
    assert all(event.context.attempt_id == claimed.attempt.attempt_id for event in events)
