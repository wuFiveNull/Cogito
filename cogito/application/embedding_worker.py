# cogito/application/embedding_worker.py
#
# EmbeddingWorker — async background worker that processes pending
# embedding_jobs and writes computed vectors back to memories.
#
# The PersistencePhase creates embedding_jobs when the EmbeddingPort
# is unavailable or times out during the turn.  This worker runs
# asynchronously (e.g. on a timer, or triggered by an event) to
# fulfill those jobs.
#
# Lifecycle:
#   pending → processing → done
#                       ↘ failed (retry on next run)

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from cogito.database.connection import AsyncDatabase
from cogito.llm.embedding import Embedder

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BATCH_SIZE = 10
_RETRY_DELAY_S = 2.0


class EmbeddingWorker:
    """Background worker that processes pending embedding jobs.

    Usage::

        worker = EmbeddingWorker(db=async_db, embedder=embedder)
        await worker.process_pending()   # one-shot
        await worker.run_forever(interval=30.0)  # polling loop
    """

    def __init__(
        self,
        db: AsyncDatabase,
        embedder: Embedder,
        *,
        model: str | None = None,
        batch_size: int = _BATCH_SIZE,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        self._db = db
        self._embedder = embedder
        self._model = model
        self._batch_size = batch_size
        self._max_attempts = max_attempts

    async def process_pending(self) -> int:
        """Process all pending embedding jobs.

        Returns:
            Number of successfully completed jobs.
        """
        jobs = await self._fetch_pending_jobs()
        if not jobs:
            return 0

        completed = 0
        for job in jobs:
            try:
                success = await self._process_one(job)
                if success:
                    completed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Embedding job failed",
                    extra={"job_id": job["id"], "memory_id": job["memory_id"]},
                )
        return completed

    async def run_forever(self, *, interval: float = 30.0) -> None:
        """Run the worker in a continuous polling loop."""
        logger.info("EmbeddingWorker started (interval=%ss)", interval)
        while True:
            try:
                count = await self.process_pending()
                if count:
                    logger.info("EmbeddingWorker completed %d jobs", count)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("EmbeddingWorker cycle failed")
            await asyncio.sleep(interval)

    # ── Internal ────────────────────────────────────────────────────

    async def _fetch_pending_jobs(self) -> list[dict[str, Any]]:
        """Fetch pending (or failed, retryable) embedding jobs."""
        return await self._db.fetchall(
            """SELECT ej.id, ej.memory_id, ej.embedding_model,
                      ej.attempts, ej.status,
                      m.content, m.embedding_dim
               FROM embedding_jobs ej
               JOIN memories m ON m.id = ej.memory_id
               WHERE ej.status IN ('pending', 'failed')
                 AND ej.attempts < :max_attempts
               ORDER BY ej.available_at ASC
               LIMIT :limit""",
            {"max_attempts": self._max_attempts, "limit": self._batch_size},
        )

    async def _process_one(self, job: dict[str, Any]) -> bool:
        """Process a single embedding job.

        Steps:
          1. Mark job as 'processing'
          2. Call embedder.embed(job.content)
          3. Write BLOB to memories.embedding
          4. Mark job as 'done'

        On failure:
          - Increment attempts
          - Set last_error
          - Mark as 'failed' (will retry next cycle if attempts < max)
        """
        job_id = job["id"]
        memory_id = job["memory_id"]
        content = job.get("content", "")

        if not content:
            await self._mark_failed(job_id, "empty_content")
            return False

        # Mark as processing
        await self._db.execute(
            "UPDATE embedding_jobs SET status = 'processing' WHERE id = :id",
            {"id": job_id},
        )

        try:
            vector = await self._embedder.embed(content)
        except Exception as exc:
            error_msg = str(exc)[:200]
            await self._mark_failed(job_id, error_msg)
            logger.warning("Embedding failed for job %s: %s", job_id, error_msg)
            return False

        if not vector:
            await self._mark_failed(job_id, "empty_vector")
            return False

        # Encode float32 vector as little-endian BLOB
        import struct

        blob = struct.pack(f"<{len(vector)}f", *vector)
        dim = len(vector)

        # Write embedding BLOB to memories, then mark job done
        await self._db.execute(
            "UPDATE memories SET embedding = :blob, embedding_dim = :dim, "
            "embedding_model = :model, embedding_format = 'float32-le' "
            "WHERE id = :mid",
            {
                "blob": blob,
                "dim": dim,
                "model": self._model or job.get("embedding_model", "default"),
                "mid": memory_id,
            },
        )

        await self._db.execute(
            "UPDATE embedding_jobs SET status = 'done', attempts = attempts + 1 "
            "WHERE id = :id",
            {"id": job_id},
        )

        return True

    async def _mark_failed(self, job_id: str, error: str) -> None:
        """Mark a job as failed with error message."""
        await self._db.execute(
            "UPDATE embedding_jobs SET status = 'failed', "
            "attempts = attempts + 1, last_error = :error "
            "WHERE id = :id",
            {"id": job_id, "error": error[:500]},
        )
