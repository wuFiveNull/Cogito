"""
Tests for the EmbeddingWorker background service.

Uses a real SQLite database with the embedding_jobs schema
and a mock Embedder to simulate vector generation.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock

import pytest

from cogito.application.embedding_worker import EmbeddingWorker
from cogito.database import AsyncDatabase, run_migrations
from cogito.database.ids import new_uuid


@pytest.fixture
async def db():
    tmp = tempfile.mktemp(suffix=".db")
    db = AsyncDatabase(tmp)
    await db.open()
    await run_migrations(db)
    yield db
    await db.close()
    try:
        os.unlink(tmp)
    except OSError:
        pass


@pytest.fixture
def mock_embedder():
    m = AsyncMock()
    m.embed.return_value = [0.1, 0.2, 0.3, 0.4]
    return m


async def seed_job(db, status="pending", content="test memory",
                   memory_id=None, attempts=0):
    """Insert a pending embedding_job with a corresponding memory row."""
    if memory_id is None:
        memory_id = new_uuid()
    # Create memory
    await db.execute(
        "INSERT INTO memories (id, user_id, memory_type, memory_key, content) "
        "VALUES (:id, :uid, 'fact', :key, :content)",
        {"id": memory_id, "uid": "u1", "key": f"test.{new_uuid()}", "content": content},
    )
    # Create job
    job_id = new_uuid()
    await db.execute(
        "INSERT INTO embedding_jobs (id, memory_id, embedding_model, status, attempts) "
        "VALUES (:id, :mid, 'test-model', :status, :attempts)",
        {"id": job_id, "mid": memory_id, "status": status, "attempts": attempts},
    )
    return job_id, memory_id


class TestEmbeddingWorker:

    @pytest.mark.asyncio
    async def test_process_pending_success(self, db, mock_embedder):
        """A pending job should be processed and marked done."""
        job_id, mem_id = await seed_job(db)
        worker = EmbeddingWorker(db, mock_embedder, model="test-model")

        count = await worker.process_pending()

        assert count == 1
        assert mock_embedder.embed.called

        # Job should be done
        row = await db.fetchone(
            "SELECT status FROM embedding_jobs WHERE id = :id",
            {"id": job_id},
        )
        assert row["status"] == "done"

        # Memory should have embedding
        mem = await db.fetchone(
            "SELECT embedding, embedding_dim, embedding_model FROM memories WHERE id = :id",
            {"id": mem_id},
        )
        assert mem["embedding"] is not None
        assert mem["embedding_dim"] == 4
        assert mem["embedding_model"] == "test-model"

    @pytest.mark.asyncio
    async def test_no_pending_jobs(self, db, mock_embedder):
        """No pending jobs → worker returns 0."""
        worker = EmbeddingWorker(db, mock_embedder)
        count = await worker.process_pending()
        assert count == 0
        assert not mock_embedder.embed.called

    @pytest.mark.asyncio
    async def test_embedder_failure(self, db, mock_embedder):
        """If embedder raises, job is marked failed and retries."""
        mock_embedder.embed.side_effect = RuntimeError("API timeout")
        job_id, _ = await seed_job(db)
        worker = EmbeddingWorker(db, mock_embedder, max_attempts=3)

        count = await worker.process_pending()

        assert count == 0
        row = await db.fetchone(
            "SELECT status, attempts, last_error FROM embedding_jobs WHERE id = :id",
            {"id": job_id},
        )
        assert row["status"] == "failed"
        assert row["attempts"] == 1

    @pytest.mark.asyncio
    async def test_empty_content_job(self, db, mock_embedder):
        """A job with empty content should be failed immediately."""
        job_id, _ = await seed_job(db, content="")
        worker = EmbeddingWorker(db, mock_embedder)

        count = await worker.process_pending()

        assert count == 0
        row = await db.fetchone(
            "SELECT status, last_error FROM embedding_jobs WHERE id = :id",
            {"id": job_id},
        )
        assert row["status"] == "failed"

    @pytest.mark.asyncio
    async def test_skip_done_jobs(self, db, mock_embedder):
        """Jobs already marked 'done' should be skipped."""
        job_id, _ = await seed_job(db, status="done")
        worker = EmbeddingWorker(db, mock_embedder, max_attempts=3)

        count = await worker.process_pending()

        assert count == 0
        assert not mock_embedder.embed.called

    @pytest.mark.asyncio
    async def test_failed_job_retries(self, db, mock_embedder):
        """A previously failed job should be retried on the next cycle."""
        mock_embedder.embed.side_effect = RuntimeError("first fail")
        job_id, _ = await seed_job(db, status="failed", attempts=1)
        worker = EmbeddingWorker(db, mock_embedder, max_attempts=3)

        # First cycle: embedder fails → job marked failed again
        count1 = await worker.process_pending()
        assert count1 == 0

        row = await db.fetchone(
            "SELECT status, attempts FROM embedding_jobs WHERE id = :id",
            {"id": job_id},
        )
        assert row["status"] == "failed"
        assert row["attempts"] == 2

        # Second cycle: embedder now succeeds
        mock_embedder.embed.side_effect = None
        mock_embedder.embed.return_value = [0.5, 0.6, 0.7]

        count2 = await worker.process_pending()
        assert count2 == 1

        row = await db.fetchone(
            "SELECT status FROM embedding_jobs WHERE id = :id",
            {"id": job_id},
        )
        assert row["status"] == "done"

    @pytest.mark.asyncio
    async def test_max_attempts_exhausted(self, db, mock_embedder):
        """A job that has exhausted max_attempts should not be queried."""
        mock_embedder.embed.side_effect = RuntimeError("fail")
        job_id, _ = await seed_job(db, status="failed", attempts=3)
        worker = EmbeddingWorker(db, mock_embedder, max_attempts=3)

        count = await worker.process_pending()

        assert count == 0
        assert not mock_embedder.embed.called

    @pytest.mark.asyncio
    async def test_batch_limit(self, db, mock_embedder):
        """Only up to batch_size jobs are fetched per cycle."""
        for _ in range(5):
            await seed_job(db)
        worker = EmbeddingWorker(db, mock_embedder, batch_size=3)

        count = await worker.process_pending()

        assert count == 3
