from __future__ import annotations

import sqlite3

from cogito.store.event_store import EventStore
from cogito.store.event_store_cutover import EventStoreCutover, assert_event_store_runtime_ready
from cogito.store.event_replay import replay_turn
from cogito.store.migration import migrate


def _legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute(
        "INSERT INTO principals (principal_id, principal_type, status, created_at) "
        "VALUES ('owner', 'owner', 'active', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO conversations (conversation_id) VALUES ('conversation-1')"
    )
    conn.execute(
        "INSERT INTO sessions (session_id, conversation_id, created_at) "
        "VALUES ('session-1', 'conversation-1', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, status, priority, version, created_at) "
        "VALUES ('turn-1', 'session-1', 'completed', 50, 2, 1704067200000)"
    )
    conn.commit()
    conn.close()


def test_cutover_builds_event_only_candidate_without_touching_source(tmp_path):
    db_path = tmp_path / "legacy.db"
    _legacy_db(db_path)

    report = EventStoreCutover(db_path, home=tmp_path).run()

    assert report.applied is False
    assert report.candidate_path is not None and report.candidate_path.is_file()
    assert all(report.validated.values())
    source = sqlite3.connect(db_path)
    assert source.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 1
    source.close()

    candidate = sqlite3.connect(report.candidate_path)
    candidate.row_factory = sqlite3.Row
    assert candidate.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='principals'"
    ).fetchone() is None
    assert EventStore(candidate).get("legacy:principals:owner:snapshot") is not None
    turn = replay_turn(EventStore(candidate).read_stream("turn", "turn-1"), "turn-1")
    assert turn is not None
    assert turn.status == "completed"
    assert turn.session_id == "session-1"
    assert_event_store_runtime_ready(candidate)
    candidate.close()


def test_cutover_apply_replaces_only_after_verified_candidate(tmp_path):
    db_path = tmp_path / "legacy.db"
    _legacy_db(db_path)

    report = EventStoreCutover(db_path, home=tmp_path).run(apply=True)

    assert report.applied is True
    assert report.candidate_path is None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns'"
    ).fetchone() is None
    assert EventStore(conn).get("legacy:turns:turn-1:snapshot") is not None
    assert_event_store_runtime_ready(conn)
    conn.close()
