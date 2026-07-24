"""Turn checkpoints are Event facts with restricted payload data."""

from __future__ import annotations

from cogito.infrastructure.payload_store import PayloadStore
from cogito.store.checkpoint_repo import CheckpointRepository
from cogito.store.event_store import EventStore


def test_event_checkpoint_round_trip_never_writes_checkpoint_row(in_memory_db, tmp_path):
    payload_store = PayloadStore(tmp_path / "payloads", in_memory_db)
    repository = CheckpointRepository(in_memory_db, payload_store=payload_store)
    checkpoint_id = repository.save(
        "turn-checkpoint-1",
        {"turn_id": "turn-checkpoint-1", "tool_calls": ["call-1"], "secret": "payload-only"},
    )

    assert in_memory_db.execute("SELECT COUNT(*) FROM turn_checkpoints").fetchone()[0] == 0
    event = EventStore(in_memory_db).read_stream("checkpoint", checkpoint_id)[0]
    assert event.event_type == "runtime.checkpoint.saved"
    assert event.payload_ref
    assert "payload-only" not in str(event.attributes)
    assert repository.load_latest("turn-checkpoint-1")["secret"] == "payload-only"

    repository.delete_by_turn("turn-checkpoint-1")
    assert repository.load_latest("turn-checkpoint-1") is None
    assert EventStore(in_memory_db).read_stream("checkpoint", checkpoint_id)[-1].event_type == (
        "runtime.checkpoint.invalidated"
    )
