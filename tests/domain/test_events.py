"""Tests for DomainEvent entity."""

from cogito.domain.events import DomainEvent


class TestDomainEvent:
    def test_create_default(self):
        e = DomainEvent()
        assert e.event_id is not None
        assert e.schema_version == "1.0"
        assert e.trust_label == "unverified"

    def test_create_with_values(self):
        e = DomainEvent(
            event_id="evt1",
            event_type="TurnCompleted",
            aggregate_type="turn",
            aggregate_id="t1",
            aggregate_version=2,
            payload={"result": "ok"},
        )
        assert e.event_type == "TurnCompleted"
        assert e.aggregate_id == "t1"
        assert e.payload == {"result": "ok"}

    def test_to_dict_roundtrip(self):
        e1 = DomainEvent(
            event_id="evt1",
            event_type="MemoryCandidateCreated",
            aggregate_type="memory",
            aggregate_id="mem1",
            correlation_id="corr1",
            causation_id="caus1",
        )
        d = e1.to_dict()
        e2 = DomainEvent.from_dict(d)
        assert e1 == e2
        assert e2.correlation_id == "corr1"
        assert e2.causation_id == "caus1"
