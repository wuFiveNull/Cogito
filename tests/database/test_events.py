"""
Tests for cogito.database events — EventRepository & EventService

对应设计文档第 9 节、第 10 节、第 13 节。
"""

from __future__ import annotations

import pytest

from cogito.database.ids import new_uuid
from cogito.database.repository.events import EventRepository
from cogito.database.service.event_service import EventService


class TestEventRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, db):
        repo = EventRepository(db)
        eid = new_uuid()
        row = await repo.insert({
            "id": eid,
            "user_id": "u1",
            "session_id": "s1",
            "seq_no": 1,
            "role": "user",
            "event_type": "user_message",
            "content": "Hello",
        })
        assert row["id"] == eid
        assert row["extraction_status"] == "pending"

        found = await repo.get_by_id(eid)
        assert found["content"] == "Hello"
        assert found["role"] == "user"

    @pytest.mark.asyncio
    async def test_session_events(self, db):
        repo = EventRepository(db)
        for i in range(1, 4):
            await repo.insert({
                "user_id": "u1",
                "session_id": "s2",
                "seq_no": i,
                "role": "user" if i % 2 else "assistant",
                "event_type": "user_message" if i % 2 else "assistant_message",
                "content": f"msg_{i}",
            })
        events = await repo.get_session_events("u1", "s2")
        assert len(events) == 3
        assert events[0]["seq_no"] == 1
        assert events[-1]["seq_no"] == 3

    @pytest.mark.asyncio
    async def test_last_seq_no(self, db):
        repo = EventRepository(db)
        seq = await repo.get_last_seq_no("u1", "s3")
        assert seq == 0

        await repo.insert({
            "user_id": "u1",
            "session_id": "s3",
            "seq_no": 5,
            "role": "user",
            "event_type": "user_message",
        })
        seq = await repo.get_last_seq_no("u1", "s3")
        assert seq == 5

    @pytest.mark.asyncio
    async def test_claim_extraction(self, db):
        repo = EventRepository(db)
        for i in range(1, 5):
            await repo.insert({
                "user_id": "u1",
                "session_id": "s4",
                "seq_no": i,
                "role": "user",
                "event_type": "user_message",
            })
        group_id = new_uuid()
        claimed = await repo.claim_extraction("u1", "s4", 1, 4, group_id)
        assert claimed == 4

        # Verify status changed
        events = await repo.get_by_extraction_group(group_id)
        assert len(events) == 4

    @pytest.mark.asyncio
    async def test_claim_twice_returns_zero(self, db):
        repo = EventRepository(db)
        for i in range(1, 3):
            await repo.insert({
                "user_id": "u1",
                "session_id": "s5",
                "seq_no": i,
                "role": "user",
                "event_type": "user_message",
            })
        g1 = new_uuid()
        g2 = new_uuid()
        c1 = await repo.claim_extraction("u1", "s5", 1, 3, g1)
        c2 = await repo.claim_extraction("u1", "s5", 1, 3, g2)
        assert c1 == 2
        assert c2 == 0  # already claimed

    @pytest.mark.asyncio
    async def test_complete_and_fail_extraction(self, db):
        repo = EventRepository(db)
        for i in range(1, 3):
            await repo.insert({
                "user_id": "u1",
                "session_id": "s6",
                "seq_no": i,
                "role": "user",
                "event_type": "user_message",
            })
        gid = new_uuid()
        await repo.claim_extraction("u1", "s6", 1, 2, gid)

        # Fail
        await repo.fail_extraction(gid, "Model error")
        failed = await repo.get_failed_extraction_groups()
        assert len(failed) >= 1

        # Retry
        retried = await repo.retry_failed_extraction(gid)
        assert retried >= 1

        # Complete
        await repo.complete_extraction(gid)
        events = await repo.get_by_extraction_group(gid)
        # After completion, we should still be able to read the events
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_pending_summary(self, db):
        repo = EventRepository(db)
        await repo.insert({
            "user_id": "u1",
            "session_id": "s7",
            "seq_no": 1,
            "role": "user",
            "event_type": "user_message",
        })
        summary = await repo.get_pending_extraction_summary()
        assert len(summary) >= 1
        assert summary[0]["pending_event_count"] >= 1


class TestEventService:
    @pytest.mark.asyncio
    async def test_save_user_message(self, db):
        svc = EventService(db)
        evt = await svc.save_user_message(
            user_id="u1",
            session_id="s1",
            content="Hello, I need help",
            trace_id="tr-1",
        )
        assert evt["role"] == "user"
        assert evt["event_type"] == "user_message"
        assert evt["seq_no"] == 1

    @pytest.mark.asyncio
    async def test_save_assistant_message(self, db):
        svc = EventService(db)
        evt = await svc.save_assistant_message(
            user_id="u1",
            session_id="s1",
            content="Sure, I can help!",
            trace_id="tr-1",
        )
        assert evt["role"] == "assistant"
        # Each test gets a fresh DB, so seq_no starts at 1
        assert evt["seq_no"] == 1

    @pytest.mark.asyncio
    async def test_save_tool_events(self, db):
        svc = EventService(db)
        req = await svc.save_tool_request(
            user_id="u1",
            session_id="s2",
            tool_name="weather",
            arguments={"city": "杭州"},
            trace_id="tr-2",
        )
        assert req["role"] == "tool"
        assert req["event_type"] == "tool_request"

        result = await svc.save_tool_result(
            user_id="u1",
            session_id="s2",
            tool_name="weather",
            result={"temp": 25},
            trace_id="tr-2",
        )
        assert result["event_type"] == "tool_result"

        err = await svc.save_tool_error(
            user_id="u1",
            session_id="s2",
            tool_name="weather",
            error_code="TIMEOUT",
            error_message="timeout",
            trace_id="tr-2",
        )
        assert err["event_type"] == "tool_error"

    @pytest.mark.asyncio
    async def test_claim_and_complete_extraction(self, db):
        svc = EventService(db)
        # Save events
        for i in range(3):
            await svc.save_user_message(
                user_id="u1",
                session_id="s3",
                content=f"msg_{i}",
                trace_id="tr-3",
            )

        gid = await svc.claim_extraction_group("u1", "s3", 1, 3)
        assert gid is not None

        events = await svc.get_extraction_events(gid)
        assert len(events) == 3

        await svc.complete_extraction(gid)

    @pytest.mark.asyncio
    async def test_claim_extraction_none_left(self, db):
        svc = EventService(db)
        gid = await svc.claim_extraction_group("u1", "nonexistent", 1, 10)
        assert gid is None  # no pending events
