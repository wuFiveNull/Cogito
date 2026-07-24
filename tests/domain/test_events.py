"""Tests for the one canonical durable Event contract."""

from cogito.domain.event import Event, EventClass, EventContext


class TestEvent:
    def test_roundtrip_preserves_context_and_safe_metadata(self):
        event = Event(
            event_id="evt1",
            event_type="memory.candidate.created",
            stream_type="memory",
            stream_id="mem1",
            stream_version=2,
            producer="memory-service",
            event_class=EventClass.DOMAIN,
            context=EventContext(correlation_id="corr1", causation_id="caus1"),
            attributes={"memory_id": "mem1", "confidence": 0.8},
            payload_ref="payload-1",
            payload_hash="hash-1",
            occurred_at=1_700_000_000_000,
        )
        assert Event.from_dict(event.to_dict()) == event

    def test_child_context_preserves_correlation(self):
        context = EventContext(trace_id="trace-1", correlation_id="corr-1", span_id="span-1")
        child = context.child(span_id="span-2")
        assert child.trace_id == "trace-1"
        assert child.parent_span_id == "span-1"
        assert child.causation_id == "span-1"
