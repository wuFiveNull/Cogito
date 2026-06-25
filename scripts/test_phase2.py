"""验证 Phase 2 的集成测试"""
import asyncio
import json
import os
import tempfile

from cogito.database import AsyncDatabase, run_migrations, new_uuid
from cogito.database.service.trace_service import TraceService
from cogito.database.service.event_service import EventService
from cogito.database.service.memory_writer import MemoryWriter
from cogito.database.service.memory_retriever import MemoryRetriever

TRACE_ID = "test-trace-1"
USER_ID = "test-user-1"
SESSION_ID = "test-session-1"


async def test():
    tmp = tempfile.mktemp(suffix=".db")
    db = AsyncDatabase(tmp)
    try:
        await db.open()
        await run_migrations(db)

        svc_trace = TraceService(db)
        svc_event = EventService(db)
        svc_write = MemoryWriter(db)
        svc_retrieve = MemoryRetriever(db)

        # ── Phase 2a: Trace ────────────────────────────────────────
        print("=== Phase 2a: Trace ===")
        span = await svc_trace.create_span(
            trace_id=TRACE_ID,
            user_id=USER_ID,
            session_id=SESSION_ID,
            step_type="segment",
            step_name="receive_message",
        )
        print(f"  Created span: {span['id']} ({span['step_name']})")
        assert span["status"] == "running"

        completed = await svc_trace.complete_span(
            span["id"],
            status="success",
            output_event_ids=["evt-1"],
            latency_ms=150,
        )
        print(f"  Completed span: status={completed['status']}, latency={completed['latency_ms']}ms")

        trace = await svc_trace.get_trace(TRACE_ID)
        print(f"  Trace spans: {len(trace)}")
        assert len(trace) == 1

        # ── Phase 2b: Event ────────────────────────────────────────
        print("\n=== Phase 2b: Event ===")
        evt = await svc_event.save_user_message(
            user_id=USER_ID,
            session_id=SESSION_ID,
            content="我最近搬到杭州了",
            trace_id=TRACE_ID,
        )
        print(f"  Saved event: seq_no={evt['seq_no']}, role={evt['role']}")

        reply = await svc_event.save_assistant_message(
            user_id=USER_ID,
            session_id=SESSION_ID,
            content="好的，我记住了！",
            trace_id=TRACE_ID,
        )
        print(f"  Saved reply: seq_no={reply['seq_no']}")

        tool_evt = await svc_event.save_tool_request(
            user_id=USER_ID,
            session_id=SESSION_ID,
            tool_name="weather_search",
            arguments={"city": "杭州"},
            trace_id=TRACE_ID,
        )
        print(f"  Tool request: id={tool_evt['id']}")

        # 提取组
        group_id = new_uuid()
        claimed = await svc_event.claim_extraction_group(
            USER_ID, SESSION_ID, 1, 3, group_id,
        )
        print(f"  Claimed extraction group: {claimed}")
        assert claimed == group_id

        events = await svc_event.get_extraction_events(group_id)
        print(f"  Extraction events: {len(events)}")
        assert len(events) >= 1

        # ── Phase 2c: Memory ───────────────────────────────────────
        print("\n=== Phase 2c: Memory ===")

        mem = await svc_write.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="residence.city",
            content="用户目前居住在杭州",
            value_json={"city": "杭州"},
            importance=0.85,
            confidence=0.95,
            source_event_ids=[evt["id"]],
            created_by_span_id=span["id"],
        )
        print(f"  Created memory: key={mem['memory_key']}, status={mem['status']}")
        assert mem["status"] == "active"

        # 重复写入（增强置信度）
        mem2 = await svc_write.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="residence.city",
            content="用户目前居住在杭州",
            importance=0.85,
            confidence=0.95,
        )
        print(f"  Reinforced memory: confidence={mem2['confidence']} (should be ~1.0)")
        assert mem2["confidence"] >= 0.95

        # 替代记忆（搬到上海）
        mem3 = await svc_write.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="residence.city",
            content="用户目前居住在上海",
            value_json={"city": "上海"},
            importance=0.85,
            confidence=0.95,
        )
        print(f"  Superseded: memory_key={mem3['memory_key']}, supersedes_id={mem3.get('supersedes_id')}")
        assert mem3["supersedes_id"] is not None

        # ── 检索测试 ────────────────────────────────────────────────
        print("\n=== Retrieval ===")
        result = await svc_retrieve.hybrid_retrieve(
            USER_ID,
            keywords=["杭州", "上海"],
            top_k=5,
        )
        print(f"  Hybrid retrieve result: {len(result)} memories")
        for r in result:
            print(f"    - {r['memory_key']}: {r['content']} (status={r['status']})")

        # FTS 检索
        fts_results = await svc_retrieve.retrieve_fts(
            USER_ID, "上海", "2026-06-24T12:00:00Z", limit=10,
        )
        print(f"  FTS results for '上海': {len(fts_results)}")

        print("\n** Phase 2 All tests passed! **")
    finally:
        await db.close()
        os.unlink(tmp)


asyncio.run(test())
