"""Runtime startup and interactive E2E tests (RB-A03 ~ RB-A10).

Real SQLite + Stub Provider (no network, no CLI).
Drives the runtime end-to-end through the **public Python API**
(`Config.load`, `RuntimeApplication.build`, `process_terminal_message`),
replacing the previous `python -m cogito` subprocess approach (PLAN-09 M0).
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = ROOT / "config.example.toml"

from cogito.config import Config

# In-process test runs without chdir — use absolute workspace so path
# resolution is not tied to the repo cwd.
EXAMPLE_BODY = None  # type: ignore[assignment]


def _make_example_body(workspace: Path) -> str:
    return textwrap.dedent(f"""\
    workspace_path = "{workspace.as_posix()}"
    [storage]
    db_path = "data/cogito.db"
    enable_wal = true
    busy_timeout = 5000
    payload_dir = "data/payload"
    [runtime]
    profile = "personal"
    timezone = "Asia/Shanghai"
    [interaction]
    bind_host = "127.0.0.1"
    allow_remote = false
    validate_origin = true
    [worker]
    concurrency = 1
    lease_duration_seconds = 300
    heartbeat_interval_seconds = 60
    outbox_lease_ttl_seconds = 120
    delivery_lease_ttl_seconds = 120
    recovery_grace_period_seconds = 30
""")


def _write_config(tmp: Path, body: str) -> Path:
    p = tmp / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class TestNewWorkspaceStubInteractive:
    """RB-A03 / RB-A04 / RB-A05 / RB-A06 / RB-A07 / RB-A08."""

    def test_new_workspace_migrates_and_does_one_turn(self) -> None:
        from cogito.application import RuntimeApplication

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = Config.load(_write_config(cwd, _make_example_body(cwd)))

            app = RuntimeApplication.build(cfg)
            try:
                # Send one message through the public in-process API
                reply = asyncio.run(app.process_terminal_message("hello"))
                assert isinstance(reply, str) and reply, "expected a non-empty reply"

                db_path = cwd / "data" / "cogito.db"
                assert db_path.exists()
                conn = _open_db(db_path)
                try:
                    row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
                    from cogito.store.migration import _discover

                    expected = max(mf.version for mf in _discover())
                    assert row[0] == expected, f"expected v{expected}, got v{row[0]}"

                    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
                    assert fk == [], f"FK violations: {fk}"

                    u = conn.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[
                        0
                    ]
                    assert u >= 1

                    a = conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE role='assistant'"
                    ).fetchone()[0]
                    assert a >= 1

                    t = conn.execute(
                        "SELECT status FROM turns ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                    assert t is not None
                    assert t["status"] == "completed"

                    r = conn.execute(
                        "SELECT status FROM run_attempts ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                    assert r["status"] in ("completed", "succeeded")

                    link = conn.execute(
                        "SELECT m.message_id "
                        "FROM messages m WHERE m.role='assistant' "
                        "AND m.reply_to_message_id IS NOT NULL LIMIT 1"
                    ).fetchone()
                    assert link is not None
                finally:
                    conn.close()
            finally:
                app.close()

    def test_two_turns_in_same_conversation(self) -> None:
        """RB-A05 / RB-A08 — sequence increments, Session does not bleed."""
        from cogito.application import RuntimeApplication

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = Config.load(_write_config(cwd, _make_example_body(cwd)))

            app = RuntimeApplication.build(cfg)
            try:
                r1 = asyncio.run(app.process_terminal_message("first message"))
                r2 = asyncio.run(app.process_terminal_message("second message"))
                assert isinstance(r1, str) and isinstance(r2, str)

                db_path = cwd / "data" / "cogito.db"
                conn = _open_db(db_path)
                try:
                    rows = conn.execute(
                        "SELECT receive_sequence FROM messages "
                        "WHERE role='user' ORDER BY receive_sequence"
                    ).fetchall()
                    assert len(rows) == 2
                    assert rows[1]["receive_sequence"] > rows[0]["receive_sequence"]

                    t_rows = conn.execute(
                        "SELECT COUNT(*) FROM turns WHERE status='completed'"
                    ).fetchone()[0]
                    assert t_rows >= 2
                finally:
                    conn.close()
            finally:
                app.close()

    def test_restart_preserves_history(self) -> None:
        """RB-A08 — restart preserves history, new Turns continue."""
        from cogito.application import RuntimeApplication

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg_path = _write_config(cwd, _make_example_body(cwd))

            # First run
            cfg = Config.load(cfg_path)
            app1 = RuntimeApplication.build(cfg)
            r1 = asyncio.run(app1.process_terminal_message("persist-me"))
            assert isinstance(r1, str) and r1
            app1.close()

            db_path = cwd / "data" / "cogito.db"
            conn = _open_db(db_path)
            try:
                before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                assert before >= 2
            finally:
                conn.close()

            # Second run
            cfg2 = Config.load(cfg_path)
            app2 = RuntimeApplication.build(cfg2)
            r2 = asyncio.run(app2.process_terminal_message("new-message-after-restart"))
            assert isinstance(r2, str) and r2
            app2.close()

            conn = _open_db(db_path)
            try:
                after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                assert after > before
                from cogito.store.migration import _discover

                expected = max(mf.version for mf in _discover())
                v = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]
                assert v == expected
            finally:
                conn.close()


class TestRecoveryAtStartup:
    """RB-A09 / RB-A10 / RB-A12."""

    def test_expired_turn_lease_reclaimed(self) -> None:
        """RB-A09 — startup reclaims expired running Turn lease."""
        from datetime import UTC, datetime
        from cogito.application import RuntimeApplication
        from cogito.contracts.envelope import ChannelEnvelope
        from cogito.service.inbound_service import InboundService

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = Config.load(_write_config(cwd, _make_example_body(cwd)))

            # Build app (migrates + runs recovery) to get the schema in place
            app = RuntimeApplication.build(cfg)
            app.close()

            db_path = cwd / "data" / "cogito.db"
            conn = _open_db(db_path)
            try:
                # Inject a fake owned, running Turn with an expired lease directly
                inbound = InboundService(conn)
                res = inbound.accept(
                    ChannelEnvelope(
                        channel_type="terminal",
                        channel_instance_id="terminal",
                        platform_sender_id="owner",
                        platform_conversation_id="terminal:default",
                        platform_message_id="test:1",
                        content_parts=[{"content_type": "text", "inline_data": "hi"}],
                        received_at=datetime.now(UTC).isoformat(),
                    )
                )
                turn_id = res.turn_id
                attempt_id = turn_id
                conn.execute(
                    """INSERT INTO run_attempts
                       (attempt_id, turn_id, attempt_no, status,
                        lease_expires_at, started_at, finished_at, heartbeat_at,
                        lease_version, worker_id)
                       VALUES (?, ?, 1, 'running', 1, 1, NULL, 1, 0, ?)""",
                    (attempt_id, turn_id, "stale-worker"),
                )
                conn.execute(
                    "UPDATE turns SET status='running', active_attempt_id=? WHERE turn_id=?",
                    (attempt_id, turn_id),
                )
                conn.commit()
            finally:
                conn.close()

            # New app build → recovery should run at startup
            app2 = RuntimeApplication.build(cfg)
            reply = asyncio.run(app2.process_terminal_message("post-recovery"))
            assert isinstance(reply, str)

            conn = _open_db(db_path)
            try:
                t = conn.execute("SELECT status FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
                # Accept either reclaimed (queued) or reclaimed-then-completed
                assert t["status"] != "running"
            finally:
                conn.close()
            app2.close()

    def test_close_is_idempotent(self) -> None:
        """Calling close() twice does not raise."""
        from cogito.application import RuntimeApplication

        app = RuntimeApplication.build(Config.load(EXAMPLE_CONFIG))
        app.close()
        app.close()  # must not raise

    def test_recovery_counts_are_reported(self) -> None:
        from cogito.application import RuntimeApplication

        app = RuntimeApplication.build(Config.load(EXAMPLE_CONFIG))
        try:
            counts = app.recovery_counts()
            assert set(counts.keys()) >= {
                "outbox_leases",
                "delivery_leases",
                "stale_turns",
                "stale_tasks",
            }
        finally:
            app.close()
