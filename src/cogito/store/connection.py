"""SQLite connection manager."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with recommended pragmas.

    Follows akashic-agent's pattern: synchronous SQLite with
    ``check_same_thread=False`` so it can be called from asyncio code.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


class ConnectionPool:
    """Simple thread-safe connection pool (single-connection for SQLite)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        if self._conn is None:
            with self._lock:
                if self._conn is None:
                    self._conn = get_connection(self._db_path)
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
