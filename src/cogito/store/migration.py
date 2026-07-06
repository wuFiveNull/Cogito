"""Schema migration runner."""

from __future__ import annotations

import hashlib
import sqlite3

from cogito.store.schema import SCHEMA_SQL


SCHEMA_VERSION = 1


def migrate(conn: sqlite3.Connection) -> None:
    """Run pending migrations and record the applied version."""
    current = _get_current_version(conn)

    if current < 1:
        _apply_initial(conn)


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
        (SCHEMA_VERSION, checksum),
    )
    conn.commit()
