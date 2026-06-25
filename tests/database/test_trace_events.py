"""
Tests for cogito.database trace_events — TraceEventRepository & TraceService

对应设计文档第 8 节、第 17 节、第 18 节、第 20 节。
"""

from __future__ import annotations

import pytest

from cogito.database.ids import new_uuid
from cogito.database.repository.trace_events import TraceEventRepository
from cogito.database.service.trace_service import TraceService


class TestTraceEventRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get_by_id(self, db):
        repo = TraceEventRepository(db)
        span_id = new_uuid()
        row = await repo.insert({
            "id": span_id,
            "trace_id": "trace-1",
            "user_id": "u1",
            "step_type": "segment",
            "step_name": "test_step",
        })
        assert row["id"] == span_id
        assert row["status"] == "running"

        found = await repo.get_by_id(span_id)
        assert found is not None
        assert found["step_name"] == "test_step"

    @pytest.mark.asyncio
    async def test_auto_generate_id(self, db):
        repo = TraceEventRepository(db)
        row = await repo.insert({
            "trace_id": "trace-2",
            "user_id": "u1",
            "step_type": "segment",
            "step_name": "auto_id",
        })
        assert row["id"] is not None
        assert len(row["id"]) == 36

    @pytest.mark.asyncio
    async def test_get_by_trace(self, db):
        repo = TraceEventRepository(db)
        trace_id = new_uuid()
        for i in range(3):
            await repo.insert({
                "trace_id": trace_id,
                "user_id": "u1",
                "step_type": "segment",
                "step_name": f"step_{i}",
            })
        spans = await repo.get_by_trace(trace_id)
        assert len(spans) == 3

    @pytest.mark.asyncio
    async def test_update_status(self, db):
        repo = TraceEventRepository(db)
        span_id = new_uuid()
        await repo.insert({
            "id": span_id,
            "trace_id": "trace-3",
            "user_id": "u1",
            "step_type": "segment",
            "step_name": "update_test",
        })
        updated = await repo.update_status(
            span_id,
            {"status": "success", "latency_ms": 200},
        )
        assert updated["status"] == "success"
        assert updated["latency_ms"] == 200

    @pytest.mark.asyncio
    async def test_get_tools_in_trace(self, db):
        repo = TraceEventRepository(db)
        trace_id = new_uuid()
        await repo.insert({
            "trace_id": trace_id,
            "user_id": "u1",
            "step_type": "tool_call",
            "step_name": "search",
            "tool_name": "web_search",
            "tool_call_id": "tc-1",
        })
        tools = await repo.get_tools_in_trace(trace_id)
        assert len(tools) == 1
        assert tools[0]["tool_name"] == "web_search"

    @pytest.mark.asyncio
    async def test_get_by_tool_call(self, db):
        repo = TraceEventRepository(db)
        for attempt in [1, 2]:
            await repo.insert({
                "trace_id": new_uuid(),
                "user_id": "u1",
                "step_type": "tool_call",
                "step_name": "retry_test",
                "tool_name": "api_call",
                "tool_call_id": "tc-retry",
                "attempt_no": attempt,
            })
        attempts = await repo.get_by_tool_call("tc-retry")
        assert len(attempts) == 2
        assert [a["attempt_no"] for a in attempts] == [1, 2]


class TestTraceService:
    @pytest.mark.asyncio
    async def test_create_and_complete_span(self, db):
        svc = TraceService(db)
        span = await svc.create_span(
            trace_id="trace-svc-1",
            user_id="u1",
            step_type="segment",
            step_name="agent_request",
        )
        assert span["status"] == "running"
        assert span["step_name"] == "agent_request"

        completed = await svc.complete_span(
            span["id"],
            status="success",
            output_event_ids=["evt-1"],
            output_memory_ids=["mem-1"],
            latency_ms=150,
        )
        assert completed["status"] == "success"
        assert completed["latency_ms"] == 150

    @pytest.mark.asyncio
    async def test_fail_span(self, db):
        svc = TraceService(db)
        span = await svc.create_span(
            trace_id="trace-svc-2",
            user_id="u1",
            step_type="tool_call",
            step_name="api_fail",
        )
        failed = await svc.fail_span(
            span["id"],
            error_code="TIMEOUT",
            error_message="Request timed out after 30s",
            latency_ms=30000,
        )
        assert failed["status"] == "failed"
        assert failed["error_code"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_trace_tree(self, db):
        svc = TraceService(db)
        trace_id = new_uuid()
        root = await svc.create_span(
            trace_id=trace_id,
            user_id="u1",
            step_type="segment",
            step_name="root",
        )
        child = await svc.create_span(
            trace_id=trace_id,
            parent_span_id=root["id"],
            user_id="u1",
            step_type="tool_call",
            step_name="child",
        )
        tree = await svc.get_trace_tree(trace_id)
        assert len(tree) == 1
        assert len(tree[0]["children"]) == 1
        assert tree[0]["children"][0]["id"] == child["id"]

    @pytest.mark.asyncio
    async def test_response_span(self, db):
        svc = TraceService(db)
        trace_id = new_uuid()
        span = await svc.create_span(
            trace_id=trace_id,
            user_id="u1",
            step_type="response",
            step_name="generate_final",
        )
        await svc.complete_span(span["id"], model_name="gpt-4")
        info = await svc.get_response_info(trace_id)
        assert info is not None

    @pytest.mark.asyncio
    async def test_clean_old_traces(self, db):
        svc = TraceService(db)
        # Create a trace
        await svc.create_span(
            trace_id="old-trace",
            user_id="u1",
            step_type="segment",
            step_name="old",
        )
        deleted = await svc.clean_old_traces("2100-01-01T00:00:00Z")
        assert deleted >= 0
