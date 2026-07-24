from __future__ import annotations

import json
import sqlite3

from cogito.infrastructure.payload_store import PayloadStore
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate
from cogito.store.task_checkpoint_repo import TaskCheckpoint, TaskCheckpointRepository


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def test_task_checkpoint_replays_from_event_and_guarded_payload(tmp_path) -> None:
    conn = _connection()
    try:
        payload_store = PayloadStore(tmp_path, conn)
        checkpoint = TaskCheckpoint(
            checkpoint_id="checkpoint-1",
            task_id="task-1",
            task_attempt_id="attempt-1",
            drift_run_id="drift-1",
            checkpoint_type="drift-step",
            schema_version=1,
            payload_ref="legacy-ref",
            payload_json=json.dumps(
                {"cursor": {"secret": "do-not-log"}, "step_index": 3},
                ensure_ascii=False,
            ),
            payload_hash="",
            config_version_id="config-1",
            capability_snapshot_version="capability-1",
            created_at=1_700_000_000_000,
        )

        stored = TaskCheckpointRepository(conn, payload_store=payload_store).insert(checkpoint)

        assert stored.payload_ref
        assert conn.execute("SELECT COUNT(*) FROM task_checkpoints").fetchone()[0] == 0
        event = EventStore(conn).read_stream("task_checkpoint", "checkpoint-1")[0]
        assert event.event_type == "drift.checkpoint.saved"
        assert event.context.task_id == "task-1"
        assert event.context.attempt_id == "attempt-1"
        assert event.payload_ref == stored.payload_ref
        assert "cursor" not in event.attributes
        assert "secret" not in event.attributes

        replayed = TaskCheckpointRepository(conn, payload_store=payload_store)
        assert replayed.latest_for_task("task-1").payload_json == checkpoint.payload_json
        assert replayed.latest_for_attempt("attempt-1").checkpoint_id == "checkpoint-1"
        assert [item.checkpoint_id for item in replayed.list_for_run("drift-1")] == [
            "checkpoint-1"
        ]
    finally:
        conn.close()
