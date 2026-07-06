# cogito/infrastructure/sqlite/repositories/embedding_jobs.py
#
# SQLite embedding-job repository for PersistencePhase.
#
# If the EmbeddingPort is unavailable or returns an error during
# the turn, the PersistencePhase writes embedding_job rows so that
# a background worker can retry the embedding later.

from __future__ import annotations

from cogito.database.connection import AsyncDatabase
from cogito.agent.runtime.persistence.models import EmbeddingJob


class SQLiteEmbeddingJobRepository:
    """SQLite-backed embedding-job store."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def add_many(
        self,
        jobs: tuple[EmbeddingJob, ...],
    ) -> None:
        """Insert multiple embedding-job rows.

        Each job references a ``memories.id`` via ``memory_id``,
        so the memory must exist before the job is enqueued.
        """
        for job in jobs:
            await self._db.execute(
                """INSERT OR IGNORE INTO embedding_jobs (
                       id, memory_id, embedding_model, status, attempts, last_error
                   ) VALUES (
                       :id, :memory_id, :model, :status, :attempts, :error
                   )""",
                {
                    "id": job.id,
                    "memory_id": job.memory_id,
                    "model": job.embedding_model,
                    "status": job.status,
                    "attempts": job.attempts,
                    "error": job.last_error,
                },
            )
