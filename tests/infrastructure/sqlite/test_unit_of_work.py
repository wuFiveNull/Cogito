"""
Integration tests for the SQLite PersistencePhase infrastructure.

Tests the UnitOfWork and all six repositories against a real SQLite
database (in-memory via temp file).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

from cogito.agent.runtime.persistence.models import (
    CandidateWriteAudit,
    EmbeddingJob,
    PersistedEvent,
    TurnCommitRecord,
)
from cogito.database import AsyncDatabase, run_migrations
from cogito.infrastructure.sqlite.connection import SQLiteConnectionFactory
from cogito.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork, SQLiteUnitOfWorkFactory


@pytest.fixture
async def db():
    """Create a fresh migrated database for each test."""
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
def uow_factory(db):
    """Create a UoW factory backed by the test database."""
    conn_factory = SQLiteConnectionFactory(db)
    return SQLiteUnitOfWorkFactory(conn_factory)


class TestSQLiteUnitOfWork:
    """Test the UnitOfWork lifecycle (BEGIN / COMMIT / ROLLBACK)."""

    @pytest.mark.asyncio
    async def test_uow_auto_commit_on_exit(self, uow_factory):
        """Normal exit without explicit commit should auto-commit."""
        async with uow_factory.create() as uow:
            await uow.sessions.create_if_absent(
                session_id="s1",
                user_id="u1",
                now=datetime.now(timezone.utc),
            )

        # Verify data is persisted
        async with uow_factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="s1")
            assert session is not None
            assert session.user_id == "u1"

    @pytest.mark.asyncio
    async def test_uow_rollback_on_exception(self, uow_factory):
        """Exception exit should roll back the transaction."""
        try:
            async with uow_factory.create() as uow:
                await uow.sessions.create_if_absent(
                    session_id="s2",
                    user_id="u2",
                    now=datetime.now(timezone.utc),
                )
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass

        # Verify data is NOT persisted
        async with uow_factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="s2")
            assert session is None

    @pytest.mark.asyncio
    async def test_uow_explicit_commit(self, uow_factory):
        """Explicit commit() should persist data."""
        async with uow_factory.create() as uow:
            await uow.sessions.create_if_absent(
                session_id="s3",
                user_id="u3",
                now=datetime.now(timezone.utc),
            )
            await uow.commit()

        async with uow_factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="s3")
            assert session is not None


class TestSQLiteSessionRepository:
    """Test session CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_if_absent(self, uow_factory):
        async with uow_factory.create() as uow:
            await uow.sessions.create_if_absent(
                session_id="s1", user_id="u1",
                now=datetime.now(timezone.utc),
            )
            await uow.sessions.create_if_absent(
                session_id="s1", user_id="u1",
                now=datetime.now(timezone.utc),
            )  # idempotent

    @pytest.mark.asyncio
    async def test_get_for_write(self, uow_factory):
        async with uow_factory.create() as uow:
            await uow.sessions.create_if_absent(
                session_id="s1", user_id="u1",
                now=datetime.now(timezone.utc),
            )
            session = await uow.sessions.get_for_write(session_id="s1")
            assert session is not None
            assert session.user_id == "u1"
            assert session.version == 0

    @pytest.mark.asyncio
    async def test_advance(self, uow_factory):
        async with uow_factory.create() as uow:
            await uow.sessions.create_if_absent(
                session_id="s1", user_id="u1",
                now=datetime.now(timezone.utc),
            )
            session = await uow.sessions.get_for_write(session_id="s1")
            now = datetime.now(timezone.utc)
            advanced = await uow.sessions.advance(
                session_id="s1",
                expected_version=session.version,
                consumed_sequences=3,
                last_turn_id="t1",
                last_request_id="r1",
                last_message_at=now,
            )
            assert advanced.version == session.version + 1
            assert advanced.next_seq_no == session.next_seq_no + 3


class TestSQLiteTurnCommitRepository:
    """Test turn_commit idempotency operations."""

    @pytest.mark.asyncio
    async def test_add_and_get_by_request(self, uow_factory):
        async with uow_factory.create() as uow:
            # Need a session + events first (FK constraints)
            await uow.sessions.create_if_absent(
                session_id="s1", user_id="u1",
                now=datetime.now(timezone.utc),
            )
            # Insert event records that the turn_commit references
            from cogito.agent.runtime.persistence.models import PersistedEvent
            from datetime import timezone as tz
            now = datetime.now(tz.utc)
            evt1 = PersistedEvent(
                event_id="evt1", user_id="u1", session_id="s1",
                seq_no=1, role="user", event_type="user_message",
                content="hello", content_json={},
                request_id="r1", turn_id="t1",
                extraction_status="pending", created_at=now,
            )
            evt2 = PersistedEvent(
                event_id="evt2", user_id="u1", session_id="s1",
                seq_no=2, role="assistant", event_type="assistant_message",
                content="hi", content_json={},
                request_id="r1", turn_id="t1",
                extraction_status="pending", created_at=now,
            )
            await uow.events.add_many((evt1, evt2))

            record = TurnCommitRecord(
                commit_id="c1",
                user_id="u1",
                session_id="s1",
                request_id="r1",
                turn_id="t1",
                commit_fingerprint="abc123",
                user_event_id="evt1",
                assistant_event_id="evt2",
                session_version=1,
                outcome_json='{"ok": true}',
                committed_at=now.strftime("%Y-%m-%dT%H:%M:%fZ"),
            )
            await uow.turn_commits.add(record)

        # Read back
        async with uow_factory.create() as uow:
            found = await uow.turn_commits.get_by_request(
                user_id="u1", request_id="r1",
            )
            assert found is not None
            assert found.commit_id == "c1"
            assert found.commit_fingerprint == "abc123"

    @pytest.mark.asyncio
    async def test_get_by_request_not_found(self, uow_factory):
        async with uow_factory.create() as uow:
            result = await uow.turn_commits.get_by_request(
                user_id="u1", request_id="nonexistent",
            )
            assert result is None


class TestSQLiteMemoryRepository:
    """Test memory repository delegated operations."""

    @pytest.mark.asyncio
    async def test_insert_and_get_active(self, uow_factory, db):
        from cogito.infrastructure.sqlite.repositories.memories import SQLiteMemoryRepository
        repo = SQLiteMemoryRepository(db)

        async with uow_factory.create() as uow:
            result = await repo.insert({
                "user_id": "u1",
                "memory_type": "fact",
                "memory_key": "test.key",
                "content": "test content",
                "importance": 0.8,
                "confidence": 0.9,
            })
            assert result["id"] is not None

            found = await repo.get_active_by_key(user_id="u1", memory_key="test.key")
            assert found is not None
            assert found["content"] == "test content"

    @pytest.mark.asyncio
    async def test_update_reinforcement(self, uow_factory, db):
        from cogito.infrastructure.sqlite.repositories.memories import SQLiteMemoryRepository
        repo = SQLiteMemoryRepository(db)

        # Insert trace_event first (FK constraint for span refs)
        span_id = "span-reinforce"
        await db.execute(
            "INSERT INTO trace_events (id, trace_id, user_id, step_type, step_name) "
            "VALUES (:id, :tid, :uid, 'test', 'reinforce')",
            {"id": span_id, "tid": "trace-reinforce", "uid": "u1"},
        )
        await db.commit()  # commit the auto-transaction before UoW starts

        async with uow_factory.create() as uow:
            row = await repo.insert({
                "user_id": "u1",
                "memory_type": "fact",
                "memory_key": "test.reinforce",
                "content": "original",
                "confidence": 0.8,
                "importance": 0.5,
            })

            await repo.update_reinforcement(
                memory_id=row["id"],
                confidence=0.9,
                importance=0.6,
                source_event_ids=("e1", "e2"),
                updated_by_span_id=span_id,
            )

            found = await repo.get_active_by_key(user_id="u1", memory_key="test.reinforce")
            assert found is not None
            assert found["confidence"] >= 0.85

    @pytest.mark.asyncio
    async def test_mark_superseded(self, uow_factory, db):
        from cogito.infrastructure.sqlite.repositories.memories import SQLiteMemoryRepository
        repo = SQLiteMemoryRepository(db)

        # Insert trace_event first (FK constraint)
        span_id = "span-supersede"
        await db.execute(
            "INSERT INTO trace_events (id, trace_id, user_id, step_type, step_name) "
            "VALUES (:id, :tid, :uid, 'test', 'supersede')",
            {"id": span_id, "tid": "trace-supersede", "uid": "u1"},
        )
        await db.commit()

        async with uow_factory.create() as uow:
            row = await repo.insert({
                "user_id": "u1",
                "memory_type": "fact",
                "memory_key": "test.supersede",
                "content": "old content",
                "confidence": 0.8,
            })

            await repo.mark_superseded(
                memory_id=row["id"],
                valid_until=datetime.now(timezone.utc),
                updated_by_span_id=span_id,
            )

            found = await repo.get_active_by_key(user_id="u1", memory_key="test.supersede")
            assert found is None
