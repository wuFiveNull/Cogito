"""Tests for infrastructure/tools — Checkpoint, Audit, Artifact stores."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

from cogito.agent.domain.tools import ArtifactRef
from cogito.agent.ports.tools.audit import ToolAuditRecord
from cogito.agent.ports.tools.checkpoint import LoopCheckpointRecord
from cogito.database.connection import AsyncDatabase
from cogito.database.migrations import run_migrations
from cogito.infrastructure.tools.artifact_store import FileArtifactStore
from cogito.infrastructure.tools.checkpoint_repository import SQLiteLoopCheckpointRepository
from cogito.infrastructure.tools.audit_repository import SQLiteToolAuditRepository


class TestSQLiteLoopCheckpointRepository:
    async def test_save_and_load(self, db: AsyncDatabase) -> None:
        repo = SQLiteLoopCheckpointRepository(db)
        await repo.ensure_schema()

        now = datetime.now(timezone.utc)
        record = LoopCheckpointRecord(
            checkpoint_id="cp_001",
            turn_id="turn_001",
            approval_id="apr_001",
            serialised_state=b'{"key": "value"}',
            integrity_hash="abc123",
            created_at=now,
            expires_at=None,
        )

        await repo.save(record)
        loaded = await repo.load("cp_001")
        assert loaded is not None
        assert loaded.checkpoint_id == "cp_001"
        assert loaded.approval_id == "apr_001"
        assert loaded.integrity_hash == "abc123"

    async def test_load_nonexistent(self, db: AsyncDatabase) -> None:
        repo = SQLiteLoopCheckpointRepository(db)
        await repo.ensure_schema()

        loaded = await repo.load("nonexistent")
        assert loaded is None

    async def test_delete(self, db: AsyncDatabase) -> None:
        repo = SQLiteLoopCheckpointRepository(db)
        await repo.ensure_schema()

        now = datetime.now(timezone.utc)
        record = LoopCheckpointRecord(
            checkpoint_id="cp_002",
            turn_id="turn_002",
            approval_id="apr_002",
            serialised_state=b'{"key": "value"}',
            integrity_hash="def456",
            created_at=now,
        )
        await repo.save(record)
        assert await repo.delete("cp_002") is True
        assert await repo.delete("cp_002") is False

    async def test_load_by_approval(self, db: AsyncDatabase) -> None:
        repo = SQLiteLoopCheckpointRepository(db)
        await repo.ensure_schema()

        now = datetime.now(timezone.utc)
        record = LoopCheckpointRecord(
            checkpoint_id="cp_003",
            turn_id="turn_003",
            approval_id="apr_003",
            serialised_state=b'{}',
            integrity_hash="ghi789",
            created_at=now,
        )
        await repo.save(record)
        loaded = await repo.load_by_approval("apr_003")
        assert loaded is not None
        assert loaded.checkpoint_id == "cp_003"


class TestSQLiteToolAuditRepository:
    async def test_record_and_dedup(self, db: AsyncDatabase) -> None:
        repo = SQLiteToolAuditRepository(db)
        await repo.ensure_schema()

        now = datetime.now(timezone.utc)
        record = ToolAuditRecord(
            call_id="call_001",
            turn_id="turn_001",
            tool_name="read_file",
            actor_id="actor1",
            session_id="session1",
            status="succeeded",
            risk="read_only",
            started_at=now,
            duration_ms=150,
            arguments_hash="hash123",
        )
        await repo.record(record)

        # Same record again (idempotent)
        await repo.record(record)

    async def test_record_batch(self, db: AsyncDatabase) -> None:
        repo = SQLiteToolAuditRepository(db)
        await repo.ensure_schema()

        now = datetime.now(timezone.utc)
        records = (
            ToolAuditRecord(
                call_id="call_a", turn_id="turn_1",
                tool_name="read_file", actor_id="a1",
                session_id="s1", status="succeeded",
                risk="read_only", started_at=now, duration_ms=100,
                arguments_hash="h1",
            ),
            ToolAuditRecord(
                call_id="call_b", turn_id="turn_1",
                tool_name="list_dir", actor_id="a1",
                session_id="s1", status="succeeded",
                risk="read_only", started_at=now, duration_ms=50,
                arguments_hash="h2",
            ),
        )
        await repo.record_batch(records)


class TestFileArtifactStore:
    async def test_store_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileArtifactStore(store_root=os.path.join(tmp, "artifacts"))
            data = b"hello world"

            ref = await store.store(data=data, media_type="text/plain", name="test.txt")
            assert ref.artifact_id is not None
            assert ref.size_bytes == len(data)
            assert ref.sha256 is not None

            read_data = await store.read(ref.artifact_id)
            assert read_data == data

    async def test_dedup_same_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileArtifactStore(store_root=os.path.join(tmp, "artifacts"))
            data = b"deduplicated content"

            ref1 = await store.store(data=data, media_type="text/plain")
            ref2 = await store.store(data=data, media_type="text/plain")

            assert ref1.artifact_id == ref2.artifact_id

    async def test_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileArtifactStore(store_root=os.path.join(tmp, "artifacts"))
            data = b"delete me"

            ref = await store.store(data=data, media_type="text/plain")
            assert await store.read(ref.artifact_id) is not None

            deleted = await store.delete(ref.artifact_id)
            assert deleted is True
            assert await store.read(ref.artifact_id) is None

    async def test_read_nonexistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileArtifactStore(store_root=os.path.join(tmp, "artifacts"))
            data = await store.read("nonexistent_id")
            assert data is None

    async def test_partial_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileArtifactStore(store_root=os.path.join(tmp, "artifacts"))
            data = b"0123456789"

            ref = await store.store(data=data, media_type="text/plain")

            partial = await store.read(ref.artifact_id, offset=3, limit=4)
            assert partial == b"3456"
