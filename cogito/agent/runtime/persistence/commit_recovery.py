# cogito/agent/runtime/persistence/commit_recovery.py
#
# CommitRecoveryService — handles recovery when the commit() call
# is interrupted or its outcome is unknown.
#
# The PersistencePhase shields commit() with asyncio.shield() so that
# cancellation doesn't tear down the transaction mid-write.  If the
# shielded call still raises (e.g. the connection died between
# commit and response), this service checks the turn_commits table
# to determine whether the commit actually succeeded.

from __future__ import annotations

from cogito.database.connection import AsyncDatabase
from cogito.infrastructure.sqlite.connection import SQLiteConnectionFactory
from cogito.agent.runtime.persistence.models import PersistenceOutcome, TurnCommitRecord


class CommitRecoveryService:
    """Determines whether a prior commit succeeded when the outcome is unknown.

    Uses a separate read (via the shared connection) to query
    ``turn_commits`` without relying on the transaction that may
    be in an uncertain state.
    """

    def __init__(self, connection_factory: SQLiteConnectionFactory) -> None:
        self._connection_factory = connection_factory

    async def lookup(
        self,
        *,
        user_id: str,
        request_id: str,
        expected_fingerprint: str,
    ) -> PersistenceOutcome | None:
        """Check if a successful commit exists for ``(user_id, request_id)``.

        Returns:
            The ``PersistenceOutcome`` from the prior commit if found
            and the fingerprints match.
            ``None`` if no prior commit exists.
            Raises ``ValueError`` if fingerprints don't match (idempotency conflict).
        """
        db = self._connection_factory.db

        row = await db.fetchone(
            "SELECT * FROM turn_commits "
            "WHERE user_id = :uid AND request_id = :rid",
            {"uid": user_id, "rid": request_id},
        )
        if row is None:
            return None

        stored_fingerprint = row["commit_fingerprint"]
        if stored_fingerprint != expected_fingerprint:
            raise ValueError(
                f"Idempotency conflict: user_id={user_id}, "
                f"request_id={request_id}: fingerprints differ"
            )

        # Parse outcome_json back into a PersistenceOutcome
        import json
        from datetime import datetime
        outcome_data = json.loads(row["outcome_json"])
        return PersistenceOutcome(
            commit_id=row["commit_id"],
            turn_id=row["turn_id"],
            request_id=row["request_id"],
            session_id=row["session_id"],
            committed_at=datetime.fromisoformat(row["committed_at"]),
            session_version=row["session_version"],
            summary_version=outcome_data.get("summary_version", 0),
            idempotent_replay=True,
            user_event_id=row["user_event_id"],
            assistant_event_id=row["assistant_event_id"],
            tool_event_ids=tuple(outcome_data.get("tool_event_ids", [])),
            candidate_outcomes=(),
            embedding_job_count=outcome_data.get("embedding_job_count", 0),
        )
