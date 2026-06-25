# cogito/infrastructure/sqlite/repositories/candidate_audits.py
#
# SQLite candidate-write-audit repository for PersistencePhase.
#
# Each candidate (preference, memory, summary) produces exactly one
# audit row recording what was done (applied, deduplicated, rejected, etc.).

from __future__ import annotations

from cogito.database.connection import AsyncDatabase
from cogito.agent.runtime.persistence.models import CandidateWriteAudit


class SQLiteCandidateAuditRepository:
    """SQLite-backed candidate-write-audit store."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def add_many(
        self,
        audits: tuple[CandidateWriteAudit, ...],
    ) -> None:
        """Insert multiple audit rows within the current transaction.

        Each audit refers to a parent ``turn_commits`` row via
        ``commit_id``, so this must be called after the commit record
        is written (but still within the same transaction).
        """
        for audit in audits:
            await self._db.execute(
                """INSERT INTO candidate_write_audits (
                       id, commit_id, user_id, session_id, turn_id,
                       candidate_id, candidate_type, candidate_key,
                       requested_operation, result_status,
                       target_record_id, reason_code,
                       confidence, importance,
                       source_event_ids_json, metadata_json
                   ) VALUES (
                       :id, :commit_id, :user_id, :session_id, :turn_id,
                       :candidate_id, :candidate_type, :candidate_key,
                       :requested_operation, :result_status,
                       :target_record_id, :reason_code,
                       :confidence, :importance,
                       :source_ids_json, :metadata_json
                   )""",
                {
                    "id": audit.id,
                    "commit_id": audit.commit_id,
                    "user_id": audit.user_id,
                    "session_id": audit.session_id,
                    "turn_id": audit.turn_id,
                    "candidate_id": audit.candidate_id,
                    "candidate_type": audit.candidate_type,
                    "candidate_key": audit.candidate_key,
                    "requested_operation": audit.requested_operation,
                    "result_status": audit.result_status,
                    "target_record_id": audit.target_record_id,
                    "reason_code": audit.reason_code,
                    "confidence": audit.confidence,
                    "importance": audit.importance,
                    "source_ids_json": audit.source_event_ids_json,
                    "metadata_json": audit.metadata_json,
                },
            )
