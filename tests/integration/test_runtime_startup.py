"""Runtime startup and interactive E2E tests (RB-A03 ~ RB-A10).

Real subprocess + real SQLite + Stub Provider (no network).
Drives the cogito runtime end-to-end:
- new workspace → migrate to v19 → interactive message → /quit
- second launch → history persisted + recoverable lease recovery
- idempotent close()
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

PY = sys.executable
ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = ROOT / "config.example.toml"

from cogito.config import Config


def _write_config(tmp: Path, body: str) -> Path:
    p = tmp / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


# Stub provider activates when model.main lacks model+api_key+base_url, so this
# minimal config drives the StubModelProvider with no network traffic.
EXAMPLE_BODY = textwrap.dedent("""\
    workspace_path = ".workspace"
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


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class TestNewWorkspaceStubInteractive:
    """RB-A03 / RB-A04 / RB-A05 / RB-A06 / RB-A07 / RB-A08."""

    def test_new_workspace_migrates_and_does_one_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = _write_config(cwd, EXAMPLE_BODY)

            result = subprocess.run(
                [PY, "-m", "cogito", "run", "--interactive", "--config", str(cfg)],
                cwd=str(cwd),
                input="hello\n/quit\n",
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr

            db_path = cwd / ".workspace" / "data" / "cogito.db"
            assert db_path.exists()
            conn = _open_db(db_path)
            try:
                row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
                from cogito.store.migration import _discover
                expected = max(mf.version for mf in _discover())
                assert row[0] == expected, f"expected migration v{expected}, got v{row[0]}"

                # FK check must be empty (done by migration runner)
                fk = conn.execute("PRAGMA foreign_key_check").fetchall()
                assert fk == [], f"FK violations: {fk}"

                # user message exists
                u = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE role='user'"
                ).fetchone()[0]
                assert u >= 1

                # assistant message (stub reply) exists
                a = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE role='assistant'"
                ).fetchone()[0]
                assert a >= 1

                # Turn completed
                t = conn.execute(
                    "SELECT status FROM turns ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                assert t is not None
                assert t["status"] == "completed"

                # RunAttempt completed
                r = conn.execute(
                    "SELECT status FROM run_attempts ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                assert r["status"] in ("completed", "succeeded")

                # input-reply linkage
                link = conn.execute(
                    "SELECT m.message_id "
                    "FROM messages m WHERE m.role='assistant' "
                    "AND m.reply_to_message_id IS NOT NULL LIMIT 1"
                ).fetchone()
                assert link is not None
            finally:
                conn.close()

            # Output must not contain any traceback (RB-A11).
            combined = result.stdout + result.stderr
            assert "Traceback" not in combined

    def test_two_turns_in_same_conversation(self) -> None:
        """RB-A05 / RB-A08 — 顺序递增，Session 不串。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = _write_config(cwd, EXAMPLE_BODY)

            result = subprocess.run(
                [PY, "-m", "cogito", "run", "--interactive", "--config", str(cfg)],
                cwd=str(cwd),
                input="first message\nsecond message\n/quit\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr

            db_path = cwd / ".workspace" / "data" / "cogito.db"
            conn = _open_db(db_path)
            try:
                rows = conn.execute(
                    "SELECT receive_sequence FROM messages WHERE role='user' "
                    "ORDER BY receive_sequence"
                ).fetchall()
                assert len(rows) == 2
                assert rows[1]["receive_sequence"] > rows[0]["receive_sequence"]

                # both turns completed
                t_rows = conn.execute(
                    "SELECT COUNT(*) FROM turns WHERE status='completed'"
                ).fetchone()[0]
                assert t_rows >= 2
            finally:
                conn.close()

    def test_eof_also_exits_cleanly(self) -> None:
        """RB-A07 — EOF (no /quit) → exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = _write_config(cwd, EXAMPLE_BODY)

            result = subprocess.run(
                [PY, "-m", "cogito", "run", "--interactive", "--config", str(cfg)],
                cwd=str(cwd),
                input="bye",  # no newline, no /quit → EOF
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr

    def test_restart_preserves_history(self) -> None:
        """RB-A08 — 重启后历史和 Memory 保留，可继续新 Turn。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = _write_config(cwd, EXAMPLE_BODY)

            r1 = subprocess.run(
                [PY, "-m", "cogito", "run", "--interactive", "--config", str(cfg)],
                cwd=str(cwd),
                input="persist-me\n/quit\n",
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert r1.returncode == 0, r1.stderr

            db_path = cwd / ".workspace" / "data" / "cogito.db"
            conn = _open_db(db_path)
            try:
                before = conn.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
                assert before >= 2  # user + assistant
            finally:
                conn.close()

            r2 = subprocess.run(
                [PY, "-m", "cogito", "run", "--interactive", "--config", str(cfg)],
                cwd=str(cwd),
                input="new-message-after-restart\n/quit\n",
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert r2.returncode == 0, r2.stderr

            conn = _open_db(db_path)
            try:
                after = conn.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
                # at least one new user + one new assistant (stub reply may skip memory msg)
                assert after > before
                # schema version matches all applied migrations
                from cogito.store.migration import _discover
                expected = max(mf.version for mf in _discover())
                v = conn.execute(
                    "SELECT MAX(version) FROM _schema_version"
                ).fetchone()[0]
                assert v == expected
            finally:
                conn.close()


class TestRecoveryAtStartup:
    """RB-A09 / RB-A10 / RB-A12."""

    def test_expired_turn_lease_reclaimed(self) -> None:
        """RB-A09 — 启动时 reclaim expired running Turn lease."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = _write_config(cwd, EXAMPLE_BODY)
            # init workspace through normal CLI
            subprocess.run(
                [PY, "-m", "cogito", "init", "--config", str(cfg)],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=20,
            )

            db_path = cwd / ".workspace" / "data" / "cogito.db"
            conn = _open_db(db_path)
            try:
                # build a fake owned, running Turn with expired lease
                from cogito.contracts.envelope import ChannelEnvelope
                from cogito.service.inbound_service import InboundService
                from datetime import UTC, datetime

                inbound = InboundService(conn)
                res = inbound.accept(ChannelEnvelope(
                    channel_type="terminal",
                    channel_instance_id="terminal",
                    platform_sender_id="owner",
                    platform_conversation_id="terminal:default",
                    platform_message_id="test:1",
                    content_parts=[{"content_type": "text", "inline_data": "hi"}],
                    received_at=datetime.now(UTC).isoformat(),
                ))
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

            # Now start the runtime — recovery should run at startup
            r = subprocess.run(
                [PY, "-m", "cogito", "run", "--interactive", "--config", str(cfg)],
                cwd=str(cwd),
                input="post-recovery\n/quit\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert r.returncode == 0, r.stderr

            conn = _open_db(db_path)
            try:
                # The fake running Turn should have been reclaimed → status=queued
                t = conn.execute(
                    "SELECT status FROM turns WHERE turn_id=?",
                    (turn_id,),
                ).fetchone()
                # After recovery + run_once, it is either queued (reclaimed-first)
                # or completed (reclaimed-then-processed). Both are acceptable;
                # what matters is it's not stuck as 'running' with expired lease.
                assert t["status"] != "running"
            finally:
                conn.close()

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
            # New workspace has no stale state; counts dict keys must be present
            counts = app.recovery_counts()
            assert set(counts.keys()) >= {"outbox_leases", "delivery_leases", "stale_turns", "stale_tasks"}
        finally:
            app.close()

    def test_invalid_config_exits_2(self) -> None:
        """RB-A11 — config problem must fail fast, never leak secret."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            bad = cwd / "config.toml"
            bad.write_text(
                textwrap.dedent("""\
                workspace_path = ".workspace"
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
                [model]
                provider = "openai_compat"
                enable_thinking = true
                api_key = "sk-this-is-a-test-secret-123"
                """),
                encoding="utf-8",
            )
            result = subprocess.run(
                [PY, "-m", "cogito", "run", "--interactive", "--config", str(bad)],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 2, result.stderr
            combined = result.stdout + result.stderr
            # Secret must not leak into any output surface
            assert "sk-this-is-a-test-secret-123" not in combined
