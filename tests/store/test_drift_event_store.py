from __future__ import annotations

import sqlite3

from cogito.store.drift_repo import DriftRunRepository, DriftSkillStateRepository
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate


def test_drift_run_replays_admission_progress_and_completion_without_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    try:
        repository = DriftRunRepository(conn, event_sourced=True)
        run_id = repository.insert(
            task_id="task-drift-1",
            principal_id="owner",
            skill_name="policy-audit",
            skill_version="1.0",
            admission_snapshot={"sensitive": "kept-out-of-event"},
            selector_version="weights-v1",
        )
        repository.update_progress(run_id, budget_used={"tool_calls": 2}, steps_taken=3)
        repository.update_status(
            run_id,
            "completed",
            finish_summary="completed safely",
            result_ref="checkpoint-payload-1",
        )

        assert conn.execute("SELECT COUNT(*) FROM drift_runs").fetchone()[0] == 0
        replayed = repository.get(run_id)
        assert replayed is not None
        assert replayed["status"] == "completed"
        assert replayed["steps_taken"] == 3
        assert replayed["result_ref"] == "checkpoint-payload-1"
        events = EventStore(conn).read_stream("drift_run", run_id)
        assert [event.event_type for event in events] == [
            "drift.run.admitted",
            "drift.run.progress.recorded",
            "drift.run.completed",
        ]
        assert "sensitive" not in events[0].attributes

        skill_state = DriftSkillStateRepository(conn, event_sourced=True)
        skill_state.upsert(
            "owner",
            "policy-audit",
            "1.0",
            last_status="completed",
            last_run_at=1_700_000_000_000,
            run_count=1,
        )
        assert conn.execute("SELECT COUNT(*) FROM drift_skill_state").fetchone()[0] == 0
        assert skill_state.get("owner", "policy-audit")["last_status"] == "completed"
    finally:
        conn.close()
