"""Tests for cogito.bus.events_lifecycle — lifecycle event types."""

import pytest

from cogito.bus.events import TurnContext
from cogito.bus.events_lifecycle import (
    DeliveryDead,
    DeliveryFailed,
    DeliveryRetryScheduled,
    DeliveryStarted,
    DeliverySucceeded,
    InboundAccepted,
    InboundDuplicateIgnored,
    InboundReceived,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    LifecycleEvent,
    OutboundAccepted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallStarted,
    TurnCancelled,
    TurnCancelRequested,
    TurnCommitted,
    TurnCommitting,
    TurnFailed,
    TurnQueued,
    TurnStarted,
)


def _make_context(**kwargs) -> TurnContext:
    from datetime import datetime
    return TurnContext(
        turn_id=kwargs.get("turn_id", "turn-1"),
        trace_id=kwargs.get("trace_id", "trace-1"),
        session_key=kwargs.get("session_key", "sess-1"),
        trigger_message_id=kwargs.get("trigger_message_id", "msg-1"),
        origin=kwargs.get("origin", "inbound"),
        started_at=kwargs.get("started_at", datetime.now()),
    )


class TestLifecycleEvent:
    def test_base_event(self):
        event = LifecycleEvent(
            event_type="test",
            trace_id="trace-1",
            session_key="sess-1",
        )
        assert event.event_id
        assert event.event_type == "test"
        assert event.trace_id == "trace-1"
        assert event.session_key == "sess-1"
        assert event.occurred_at is not None

    def test_frozen(self):
        event = LifecycleEvent(event_type="test")
        with pytest.raises(AttributeError):
            event.event_type = "changed"


class TestInboundEvents:
    def test_received(self):
        e = InboundReceived(trace_id="tr", message_id="msg-1")
        assert e.event_type == "inbound_received"
        assert e.message_id == "msg-1"

    def test_accepted(self):
        e = InboundAccepted(trace_id="tr")
        assert e.event_type == "inbound_accepted"

    def test_duplicate_ignored(self):
        e = InboundDuplicateIgnored(
            trace_id="tr",
            message_id="msg-dup",
            existing_message_id="msg-orig",
        )
        assert e.event_type == "inbound_duplicate_ignored"
        assert e.existing_message_id == "msg-orig"


class TestTurnEvents:
    def test_queued(self):
        e = TurnQueued(trace_id="tr", turn_id="t-1")
        assert e.event_type == "turn_queued"
        assert e.turn_id == "t-1"

    def test_started(self):
        e = TurnStarted(trace_id="tr", turn_id="t-1")
        assert e.event_type == "turn_started"

    def test_started_from_context(self):
        ctx = _make_context()
        e = TurnStarted.from_context(ctx)
        assert e.trace_id == ctx.trace_id
        assert e.session_key == ctx.session_key
        assert e.turn_id == ctx.turn_id
        assert e.message_id == ctx.trigger_message_id

    def test_cancel_requested(self):
        e = TurnCancelRequested(trace_id="tr", turn_id="t-1")
        assert e.event_type == "turn_cancel_requested"

    def test_cancelled(self):
        e = TurnCancelled(trace_id="tr", turn_id="t-1")
        assert e.event_type == "turn_cancelled"

    def test_failed(self):
        e = TurnFailed(trace_id="tr", turn_id="t-1", error="timeout")
        assert e.event_type == "turn_failed"
        assert e.error == "timeout"


class TestLLMEvents:
    def test_started(self):
        e = LLMCallStarted(trace_id="tr", model="claude-sonnet-4-6")
        assert e.event_type == "llm_call_started"
        assert e.model == "claude-sonnet-4-6"

    def test_completed(self):
        e = LLMCallCompleted(
            trace_id="tr", model="claude-sonnet-4-6",
            input_tokens=100, output_tokens=50,
        )
        assert e.input_tokens == 100
        assert e.output_tokens == 50

    def test_failed(self):
        e = LLMCallFailed(trace_id="tr", model="claude-sonnet-4-6", error="rate_limit")
        assert e.error == "rate_limit"


class TestToolEvents:
    def test_started(self):
        e = ToolCallStarted(trace_id="tr", tool_name="get_weather")
        assert e.event_type == "tool_call_started"
        assert e.tool_name == "get_weather"

    def test_completed(self):
        e = ToolCallCompleted(trace_id="tr", tool_name="get_weather", duration_ms=150.0)
        assert e.duration_ms == 150.0

    def test_failed(self):
        e = ToolCallFailed(trace_id="tr", tool_name="get_weather", error="api_down")
        assert e.error == "api_down"


class TestCommitEvents:
    def test_committing(self):
        e = TurnCommitting(trace_id="tr", turn_id="t-1")
        assert e.event_type == "turn_committing"

    def test_committed(self):
        e = TurnCommitted(trace_id="tr", turn_id="t-1")
        assert e.event_type == "turn_committed"

    def test_committed_from_context(self):
        ctx = _make_context()
        e = TurnCommitted.from_context(ctx)
        assert e.trace_id == ctx.trace_id
        assert e.session_key == ctx.session_key
        assert e.turn_id == ctx.turn_id


class TestDeliveryEvents:
    def test_accepted(self):
        e = OutboundAccepted(trace_id="tr", outbound_id="out-1")
        assert e.event_type == "outbound_accepted"
        assert e.outbound_id == "out-1"

    def test_started(self):
        e = DeliveryStarted(trace_id="tr", outbound_id="out-1")
        assert e.event_type == "delivery_started"

    def test_succeeded(self):
        e = DeliverySucceeded(
            trace_id="tr", outbound_id="out-1",
            external_message_id="ext-100",
        )
        assert e.external_message_id == "ext-100"

    def test_retry_scheduled(self):
        from datetime import datetime
        e = DeliveryRetryScheduled(
            trace_id="tr", outbound_id="out-1",
            attempt=2, next_attempt_at=datetime(2026, 6, 23, 13, 0, 0),
        )
        assert e.attempt == 2
        assert e.next_attempt_at is not None

    def test_failed(self):
        e = DeliveryFailed(
            trace_id="tr", outbound_id="out-1",
            attempt=3, error_code="429", error_message="rate limited",
        )
        assert e.attempt == 3
        assert e.error_code == "429"

    def test_dead(self):
        e = DeliveryDead(
            trace_id="tr", outbound_id="out-1",
            error_code="perm_denied", error_message="bot blocked",
        )
        assert e.error_code == "perm_denied"
