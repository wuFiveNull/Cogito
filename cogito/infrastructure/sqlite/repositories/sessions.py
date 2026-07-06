# cogito/infrastructure/sqlite/repositories/sessions.py
#
# SQLite session repository for PersistencePhase.
#
# Manages the ``sessions`` control table: create-if-absent, read,
# advance (version + next_seq_no), and update summary.
# All operations are designed to be called within a UoW transaction.

from __future__ import annotations

from datetime import datetime

from cogito.database.connection import AsyncDatabase
from cogito.agent.runtime.persistence.models import SessionSnapshot


class SQLiteSessionRepository:
    """SQLite-backed session store for the PersistencePhase UoW."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def create_if_absent(
        self,
        *,
        session_id: str,
        user_id: str,
        now: datetime,
    ) -> None:
        """Ensure a session row exists (INSERT … ON CONFLICT DO NOTHING)."""
        now_str = now.strftime("%Y-%m-%dT%H:%M:%fZ")
        await self._db.execute(
            """INSERT INTO sessions (session_id, user_id, created_at, updated_at)
               VALUES (:sid, :uid, :now, :now)
               ON CONFLICT(session_id) DO NOTHING""",
            {"sid": session_id, "uid": user_id, "now": now_str},
        )

    async def get_for_write(
        self,
        *,
        session_id: str,
    ) -> SessionSnapshot | None:
        """Read the session row for update (within an active transaction)."""
        row = await self._db.fetchone(
            "SELECT * FROM sessions WHERE session_id = :sid",
            {"sid": session_id},
        )
        if row is None:
            return None
        return SessionSnapshot(
            session_id=row["session_id"],
            user_id=row["user_id"],
            version=row["version"],
            next_seq_no=row["next_seq_no"],
            summary_text=row.get("summary_text"),
            summary_version=row.get("summary_version", 0),
            summary_updated_at=row.get("summary_updated_at"),
            last_turn_id=row.get("last_turn_id"),
            last_request_id=row.get("last_request_id"),
            last_message_at=row.get("last_message_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    async def advance(
        self,
        *,
        session_id: str,
        expected_version: int,
        consumed_sequences: int,
        last_turn_id: str,
        last_request_id: str,
        last_message_at: datetime,
    ) -> SessionSnapshot:
        """Atomically advance session version and next_seq_no.

        Uses ``UPDATE … WHERE version = :expected_version … RETURNING *``
        for optimistic concurrency control.  Returns the updated snapshot
        or raises if the version doesn't match.
        """
        now_str = last_message_at.strftime("%Y-%m-%dT%H:%M:%fZ")
        row = await self._db.fetchone(
            """UPDATE sessions
               SET version = version + 1,
                   next_seq_no = next_seq_no + :consumed,
                   last_turn_id = :turn_id,
                   last_request_id = :request_id,
                   last_message_at = :msg_at
               WHERE session_id = :sid
                 AND version = :expected
               RETURNING *""",
            {
                "sid": session_id,
                "expected": expected_version,
                "consumed": consumed_sequences,
                "turn_id": last_turn_id,
                "request_id": last_request_id,
                "msg_at": now_str,
            },
        )
        if row is None:
            raise RuntimeError(
                f"Session {session_id} version conflict: "
                f"expected {expected_version}"
            )
        return SessionSnapshot(
            session_id=row["session_id"],
            user_id=row["user_id"],
            version=row["version"],
            next_seq_no=row["next_seq_no"],
            summary_text=row.get("summary_text"),
            summary_version=row.get("summary_version", 0),
            last_turn_id=row.get("last_turn_id"),
            last_request_id=row.get("last_request_id"),
            last_message_at=row.get("last_message_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    async def update_summary(
        self,
        *,
        session_id: str,
        content: str,
        expected_summary_version: int,
        now: datetime,
    ) -> SessionSnapshot:
        """Update session summary text and bump summary_version.

        Uses ``WHERE summary_version = :expected`` for optimistic
        concurrency.  Returns the updated snapshot.
        """
        now_str = now.strftime("%Y-%m-%dT%H:%M:%fZ")
        row = await self._db.fetchone(
            """UPDATE sessions
               SET summary_text = :content,
                   summary_version = summary_version + 1,
                   summary_updated_at = :now
               WHERE session_id = :sid
                 AND summary_version = :expected
               RETURNING *""",
            {
                "sid": session_id,
                "content": content,
                "expected": expected_summary_version,
                "now": now_str,
            },
        )
        if row is None:
            raise RuntimeError(
                f"Session {session_id} summary version conflict: "
                f"expected {expected_summary_version}"
            )
        return SessionSnapshot(
            session_id=row["session_id"],
            user_id=row["user_id"],
            version=row["version"],
            next_seq_no=row["next_seq_no"],
            summary_text=row.get("summary_text"),
            summary_version=row.get("summary_version", 0),
            summary_updated_at=row.get("summary_updated_at"),
            last_turn_id=row.get("last_turn_id"),
            last_request_id=row.get("last_request_id"),
            last_message_at=row.get("last_message_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
