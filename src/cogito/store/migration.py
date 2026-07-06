"""Schema migration runner."""

from __future__ import annotations

import hashlib
import sqlite3

from cogito.store.schema import SCHEMA_SQL


SCHEMA_VERSION = 2


def migrate(conn: sqlite3.Connection) -> None:
    """Run pending migrations and record the applied version."""
    current = _get_current_version(conn)

    if current < 1:
        _apply_initial(conn)

    if current < 2:
        _apply_v2(conn)


def _get_current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        return row[0] if row and row[0] else 0
    except sqlite3.OperationalError:
        return 0


def _apply_initial(conn: sqlite3.Connection) -> None:
    checksum = hashlib.sha256(SCHEMA_SQL.encode()).hexdigest()[:16]
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO _schema_version (version, checksum) VALUES (?, ?)",
        (1, checksum),
    )
    conn.commit()


def _apply_v2(conn: sqlite3.Connection) -> None:
    """Migrate v1 → v2: add input_message_id and version columns to turns, expand status options."""
    # SQLite doesn't support ALTER TABLE for CHECK constraint changes, so we
    # rebuild the table. Disable FK checks temporarily for the rename cycle.
    conn.executescript("""
        PRAGMA foreign_keys=OFF;

        ALTER TABLE turns RENAME TO turns_v1;

        CREATE TABLE turns (
            turn_id             TEXT PRIMARY KEY,
            session_id          TEXT NOT NULL DEFAULT '',
            input_message_id    TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted','queued','running','waiting_user','waiting_external','completed','cancelled','failed')),
            priority            INTEGER NOT NULL DEFAULT 80,
            version             INTEGER NOT NULL DEFAULT 1,
            cancel_requested_at TEXT,
            active_attempt_id   TEXT,
            final_message_id    TEXT,
            created_at          TEXT NOT NULL
        );

        INSERT INTO turns (turn_id, session_id, status, priority, cancel_requested_at, active_attempt_id, final_message_id, created_at)
            SELECT turn_id, session_id,
                   CASE WHEN status = 'created' THEN 'accepted' ELSE status END,
                   priority, cancel_requested_at, active_attempt_id, final_message_id, created_at
            FROM turns_v1;

        DROP TABLE turns_v1;

        PRAGMA foreign_keys=ON;
    """)
    checksum = hashlib.sha256(SCHEMA_SQL.encode()).hexdigest()[:16]
    conn.execute(
        "INSERT INTO _schema_version (version, checksum) VALUES (?, ?)",
        (2, checksum),
    )
    conn.commit()
