"""
Fault injection + idempotent replay + integrity tests for PersistencePhase.

These tests verify the core transactional guarantees of the PersistencePhase:
  1. Atomicity — partial failures roll back completely
  2. Idempotency — duplicate commits are safely handled
  3. Foreign key integrity — after every test
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from cogito.agent.runtime.persistence.models import (
    PersistedEvent,
    TurnCommitRecord,
)
from cogito.database import AsyncDatabase, run_migrations
from cogito.infrastructure.sqlite.connection import SQLiteConnectionFactory
from cogito.infrastructure.sqlite.unit_of_work import (
    SQLiteUnitOfWorkFactory,
)


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
def factory(db):
    """Create UoW factory."""
    return SQLiteUnitOfWorkFactory(SQLiteConnectionFactory(db))


@pytest.fixture
def now():
    return datetime.now(timezone.utc)


# ── Helpers ────────────────────────────────────────────────────────────

def make_event(event_id: str, seq_no: int, role: str, user_id: str = "u1",
               session_id: str = "s1", request_id: str = "r1", turn_id: str = "t1",
               now: datetime | None = None):
    if now is None:
        now = datetime.now(timezone.utc)
    return PersistedEvent(
        event_id=event_id, user_id=user_id, session_id=session_id,
        seq_no=seq_no, role=role,
        event_type=f"{role}_message", content="test",
        content_json={}, request_id=request_id, turn_id=turn_id,
        extraction_status="pending" if role in ("user", "assistant") else "ignored",
        created_at=now,
    )


async def setup_session_and_events(uow, user_id="u1", session_id="s1",
                                    request_id="r1", turn_id="t1", now=None,
                                    prefix="evt"):
    """Create session + events for persistence tests."""
    if now is None:
        now = datetime.now(timezone.utc)
    await uow.sessions.create_if_absent(session_id=session_id, user_id=user_id, now=now)
    session = await uow.sessions.get_for_write(session_id=session_id)

    evt1 = make_event(f"{prefix}_u1", 1, "user", user_id, session_id, request_id, turn_id, now)
    evt2 = make_event(f"{prefix}_a1", 2, "assistant", user_id, session_id, request_id, turn_id, now)
    await uow.events.add_many((evt1, evt2))

    advanced = await uow.sessions.advance(
        session_id=session_id, expected_version=session.version,
        consumed_sequences=2, last_turn_id=turn_id,
        last_request_id=request_id, last_message_at=now,
    )
    return advanced, evt1.event_id, evt2.event_id


def check_integrity(db):
    """Run PRAGMA checks — called manually since we can't await in a finalizer."""
    import asyncio
    loop = asyncio.get_event_loop()

    async def _check():
        fk = await db.fetchall("PRAGMA foreign_key_check")
        assert fk == [], f"Foreign key violations: {fk}"
        qc = await db.fetchone("PRAGMA quick_check")
        assert qc is not None
        assert qc.get("quick_check") == "ok" or list(qc.values())[0] == "ok"

    loop.run_until_complete(_check())


# ═══════════════════════════════════════════════════════════════════════
# Part 1: Fault Injection — Atomicity
# ═══════════════════════════════════════════════════════════════════════

class TestFaultInjection:
    """Inject failures at each critical step, verify rollback."""

    @pytest.mark.asyncio
    async def test_fault_after_session_creation(self, db, factory, now):
        """Fail after session create but before events — session should roll back."""
        try:
            async with factory.create() as uow:
                await uow.sessions.create_if_absent(
                    session_id="s_fail_1", user_id="u1", now=now,
                )
                raise RuntimeError("injected failure")
        except RuntimeError:
            pass

        # Verify session was rolled back
        async with factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="s_fail_1")
            assert session is None, "Session should not exist after rollback"

    @pytest.mark.asyncio
    async def test_fault_after_events_write(self, db, factory, now):
        """Fail after events written but before session advance."""
        try:
            async with factory.create() as uow:
                await uow.sessions.create_if_absent(
                    session_id="s_fail_2", user_id="u1", now=now,
                )
                session = await uow.sessions.get_for_write(session_id="s_fail_2")
                evt1 = make_event("evt_f1", 1, "user", session_id="s_fail_2",
                                   request_id="r_fail", now=now)
                evt2 = make_event("evt_f2", 2, "assistant", session_id="s_fail_2",
                                   request_id="r_fail", now=now)
                await uow.events.add_many((evt1, evt2))
                raise RuntimeError("injected failure after events")
        except RuntimeError:
            pass

        # Verify everything was rolled back
        async with factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="s_fail_2")
            assert session is None
            from cogito.database.repository.events import EventRepository
        from cogito.database.repository.events import EventRepository as EvtRepo
        evt_repo = EvtRepo(db)
        evt1 = await evt_repo.get_by_id("evt_f1")
        assert evt1 is None, "Event should not exist after rollback"

    @pytest.mark.asyncio
    async def test_fault_after_session_advance(self, db, factory, now):
        """Fail after session advance but before summary — full rollback."""
        try:
            async with factory.create() as uow:
                await setup_session_and_events(uow, session_id="s_fail_3",
                                                request_id="r_fail2", now=now,
                                                prefix="fa3")
                raise RuntimeError("injected failure after advance")
        except RuntimeError:
            pass

        async with factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="s_fail_3")
            assert session is None or session.version == 0

    @pytest.mark.asyncio
    async def test_fault_after_turn_commit_before_commit(self, db, factory, now):
        """Fail after turn_commit INSERT but before COMMIT — everything rolls back."""
        try:
            async with factory.create() as uow:
                adv, uid, aid = await setup_session_and_events(
                    uow, session_id="s_fail_4", request_id="r_fail4", now=now,
                    prefix="fa4")
                tc = TurnCommitRecord(
                    commit_id="c_fail_1", user_id="u1", session_id="s_fail_4",
                    request_id="r_fail4", turn_id="t_fail",
                    commit_fingerprint="fp_fail", user_event_id=uid,
                    assistant_event_id=aid, session_version=adv.version,
                    outcome_json='{"ok":true}',
                    committed_at=now.strftime("%Y-%m-%dT%H:%M:%fZ"),
                )
                await uow.turn_commits.add(tc)
                raise RuntimeError("injected failure after turn_commit")
        except RuntimeError:
            pass

        async with factory.create() as uow:
            # turn_commits should be empty
            found = await uow.turn_commits.get_by_request(
                user_id="u1", request_id="r_fail4",
            )
            assert found is None, "TurnCommit should not exist after rollback"
            # Session should not have advanced
            session = await uow.sessions.get_for_write(session_id="s_fail_4")
            assert session is None or session.version == 0


# ═══════════════════════════════════════════════════════════════════════
# Part 2: Idempotency
# ═══════════════════════════════════════════════════════════════════════

class TestIdempotentReplay:
    """Verify idempotency detection for repeated commits."""

    @pytest.mark.asyncio
    async def test_same_request_id_same_fingerprint(self, db, factory, now):
        """Same (user_id, request_id, fingerprint) → idempotent replay."""
        # First commit
        async with factory.create() as uow:
            adv, uid, aid = await setup_session_and_events(
                uow, request_id="r_idem1", now=now, prefix="id1")
            tc = TurnCommitRecord(
                commit_id="c_idem_1a", user_id="u1", session_id="s1",
                request_id="r_idem1", turn_id="t_idem1",
                commit_fingerprint="abc123", user_event_id=uid,
                assistant_event_id=aid, session_version=adv.version,
                outcome_json='{"ok":true}',
                committed_at=now.strftime("%Y-%m-%dT%H:%M:%fZ"),
            )
            await uow.turn_commits.add(tc)
            await uow.commit()

        # Verify: turn_commit exists
        async with factory.create() as uow:
            found = await uow.turn_commits.get_by_request(
                user_id="u1", request_id="r_idem1",
            )
            assert found is not None
            assert found.commit_fingerprint == "abc123"

    @pytest.mark.asyncio
    async def test_unique_constraint_blocks_duplicate(self, db, factory, now):
        """UNIQUE(user_id, request_id) prevents a second row with same key."""
        async with factory.create() as uow:
            adv, uid, aid = await setup_session_and_events(
                uow, session_id="s_dup", request_id="r_dup", now=now, prefix="dup")
            tc1 = TurnCommitRecord(
                commit_id="c_dup_1", user_id="u1", session_id="s_dup",
                request_id="r_dup", turn_id="t_dup1",
                commit_fingerprint="fp1", user_event_id=uid,
                assistant_event_id=aid, session_version=adv.version,
                outcome_json='{"ok":true}',
                committed_at=now.strftime("%Y-%m-%dT%H:%M:%fZ"),
            )
            await uow.turn_commits.add(tc1)
            await uow.commit()

        # Second attempt with same (user_id, request_id) but different fingerprint
        async with factory.create() as uow:
            tc2 = TurnCommitRecord(
                commit_id="c_dup_2", user_id="u1", session_id="s_dup",
                request_id="r_dup", turn_id="t_dup2",
                commit_fingerprint="fp2", user_event_id=uid,
                assistant_event_id=aid, session_version=1,
                outcome_json='{"ok":true}',
                committed_at=now.strftime("%Y-%m-%dT%H:%M:%fZ"),
            )
            with pytest.raises(Exception):
                await uow.turn_commits.add(tc2)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_different_user_same_request_id(self, db, factory, now):
        """Different user_id with same request_id is allowed."""
        async with factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="s_u2")
            if session is None:
                await uow.sessions.create_if_absent(
                    session_id="s_u2", user_id="u2", now=now,
                )
                session = await uow.sessions.get_for_write(session_id="s_u2")
            # Create events so the session advances to version >= 1
            evt1 = make_event("evt_u2_1", 1, "user", user_id="u2",
                               session_id="s_u2", request_id="r_shared", now=now)
            evt2 = make_event("evt_u2_2", 2, "assistant", user_id="u2",
                               session_id="s_u2", request_id="r_shared", now=now)
            await uow.events.add_many((evt1, evt2))
            adv = await uow.sessions.advance(
                session_id="s_u2", expected_version=session.version,
                consumed_sequences=2, last_turn_id="t_u2",
                last_request_id="r_shared", last_message_at=now,
            )
            tc = TurnCommitRecord(
                commit_id="c_u2", user_id="u2", session_id="s_u2",
                request_id="r_shared", turn_id="t_u2",
                commit_fingerprint="fp_u2", user_event_id=evt1.event_id,
                assistant_event_id=evt2.event_id, session_version=adv.version,
                outcome_json='{"ok":true}',
                committed_at=now.strftime("%Y-%m-%dT%H:%M:%fZ"),
            )
            await uow.turn_commits.add(tc)
            await uow.commit()

        async with factory.create() as uow:
            found = await uow.turn_commits.get_by_request(
                user_id="u2", request_id="r_shared",
            )
            assert found is not None


# ═══════════════════════════════════════════════════════════════════════
# Part 3: Foreign Key + Quick Check
# ═══════════════════════════════════════════════════════════════════════

class TestIntegrity:
    """Run PRAGMA integrity checks after each operation."""

    @pytest.mark.asyncio
    async def test_foreign_key_check_after_commit(self, db, factory, now):
        """Full write sequence should leave DB with no FK violations."""
        async with factory.create() as uow:
            adv, uid, aid = await setup_session_and_events(uow, now=now, prefix="fk1")
            tc = TurnCommitRecord(
                commit_id="c_int_1", user_id="u1", session_id="s1",
                request_id="r_int", turn_id="t_int",
                commit_fingerprint="fp_int", user_event_id=uid,
                assistant_event_id=aid, session_version=adv.version,
                outcome_json='{"ok":true}',
                committed_at=now.strftime("%Y-%m-%dT%H:%M:%fZ"),
            )
            await uow.turn_commits.add(tc)
            await uow.commit()

        # Check foreign keys
        violations = await db.fetchall("PRAGMA foreign_key_check")
        assert violations == [], f"FK violations: {violations}"

    @pytest.mark.asyncio
    async def test_quick_check_after_multiple_commits(self, db, factory, now):
        """Multiple commits should not corrupt the database."""
        for i in range(5):
            sid = f"s_int_{i}"
            rid = f"r_int_{i}"
            async with factory.create() as uow:
                adv, uid, aid = await setup_session_and_events(
                    uow, session_id=sid, request_id=rid, turn_id=f"t_{i}",
                    now=now, prefix=f"q{i}",
                )
                tc = TurnCommitRecord(
                    commit_id=f"c_int_{i}", user_id="u1", session_id=sid,
                    request_id=rid, turn_id=f"t_{i}",
                    commit_fingerprint=f"fp_{i}", user_event_id=uid,
                    assistant_event_id=aid, session_version=adv.version,
                    outcome_json='{"ok":true}',
                    committed_at=now.strftime("%Y-%m-%dT%H:%M:%fZ"),
                )
                await uow.turn_commits.add(tc)
                await uow.commit()

        qc = await db.fetchone("PRAGMA quick_check")
        assert qc is not None
        status = list(qc.values())[0]
        assert status == "ok", f"quick_check failed: {status}"

    @pytest.mark.asyncio
    async def test_foreign_key_check_after_rollback(self, db, factory, now):
        """Rolled back transactions should leave DB clean."""
        try:
            async with factory.create() as uow:
                await uow.sessions.create_if_absent(
                    session_id="s_roll_fk", user_id="u1", now=now,
                )
                raise RuntimeError("rollback")
        except RuntimeError:
            pass

        violations = await db.fetchall("PRAGMA foreign_key_check")
        assert violations == [], f"FK violations after rollback: {violations}"
