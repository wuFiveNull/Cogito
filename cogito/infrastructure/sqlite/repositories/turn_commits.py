# cogito/infrastructure/sqlite/repositories/turn_commits.py
#
# SQLite turn_commit repository for PersistencePhase.
#
# Manages the ``turn_commits`` table used for idempotency tracking.
# A turn_commit row is the anchor that proves a turn has been fully
# persisted.

from __future__ import annotations

from cogito.database.connection import AsyncDatabase
from cogito.agent.runtime.persistence.models import TurnCommitRecord


class SQLiteTurnCommitRepository:
    """SQLite-backed turn-commit store."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get_by_request(
        self,
        *,
        user_id: str,
        request_id: str,
    ) -> TurnCommitRecord | None:
        """Look up a prior commit by ``(user_id, request_id)``.

        Returns ``None`` if no prior commit exists.
        """
        row = await self._db.fetchone(
            "SELECT * FROM turn_commits "
            "WHERE user_id = :uid AND request_id = :rid",
            {"uid": user_id, "rid": request_id},
        )
        if row is None:
            return None
        return TurnCommitRecord(
            commit_id=row["commit_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            request_id=row["request_id"],
            turn_id=row["turn_id"],
            commit_fingerprint=row["commit_fingerprint"],
            user_event_id=row["user_event_id"],
            assistant_event_id=row["assistant_event_id"],
            session_version=row["session_version"],
            outcome_json=row["outcome_json"],
            persistence_span_id=row.get("persistence_span_id"),
            committed_at=row.get("committed_at"),
        )

    async def add(self, record: TurnCommitRecord) -> None:
        """Insert a new turn_commit record.

        Must be called within an active transaction.
        The ``UNIQUE(user_id, request_id)`` constraint on the
        table provides the final idempotency defence.
        """
        await self._db.execute(
            """INSERT INTO turn_commits (
                   commit_id, user_id, session_id, request_id, turn_id,
                   commit_fingerprint, user_event_id, assistant_event_id,
                   session_version, outcome_json, persistence_span_id,
                   committed_at
               ) VALUES (
                   :commit_id, :user_id, :session_id, :request_id, :turn_id,
                   :fingerprint, :user_event_id, :assistant_event_id,
                   :session_version, :outcome_json, :span_id,
                   :committed_at
               )""",
            {
                "commit_id": record.commit_id,
                "user_id": record.user_id,
                "session_id": record.session_id,
                "request_id": record.request_id,
                "turn_id": record.turn_id,
                "fingerprint": record.commit_fingerprint,
                "user_event_id": record.user_event_id,
                "assistant_event_id": record.assistant_event_id,
                "session_version": record.session_version,
                "outcome_json": record.outcome_json,
                "span_id": record.persistence_span_id,
                "committed_at": record.committed_at or "",
            },
        )
