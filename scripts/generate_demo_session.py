"""
生成模拟长 Session 数据 → 写入数据库 → 导出 JSON 预览
"""
import asyncio
import json
import os
import tempfile

from cogito.database import AsyncDatabase, run_migrations, new_uuid


async def main():
    # 使用临时数据库
    db_path = os.path.join(
        os.path.dirname(__file__) or ".",
        "demo_session.db",
    )
    db = AsyncDatabase(db_path)
    await db.open()
    await run_migrations(db)

    user_id = "user-wjh"
    session_id = "session-demo-001"
    trace_root = new_uuid()
    now = "2026-06-24T18:30:00.000Z"

    # ================================================================
    # Trace T100 — 主请求链路
    # ================================================================

    # S1: 根 span — receive user message
    s1 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, user_id, session_id,
        step_type, step_name, status, started_at, metadata_json,
        input_event_ids_json, output_event_ids_json)
        VALUES (:id, :trace_id, :user_id, :session_id,
        'input', 'receive_user_message', 'success', :started_at, '{}',
        :input_json, :output_json)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": trace_root,
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:30:00.000Z",
            "input_json": "[]", "output_json": json.dumps(["evt-001"]),
        },
    )
    s1_id = s1["id"]

    # E1: 用户消息
    e1 = await db.fetchone(
        """INSERT INTO events (id, user_id, session_id, seq_no, role,
        event_type, content, trace_id, created_by_span_id, created_at)
        VALUES (:id, :uid, :sid, 1, 'user', 'user_message',
        :content, :trace, :span_id, :created_at)
        RETURNING *""",
        {
            "id": "evt-001", "uid": user_id, "sid": session_id,
            "content": "帮我推荐一下杭州适合请客户吃饭的餐厅，要环境安静、有包间的",
            "trace": trace_root, "span_id": s1_id,
            "created_at": "2026-06-24T18:30:00.000Z",
        },
    )

    # S2: 记忆检索
    s2 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, parent_span_id,
        user_id, session_id, step_type, step_name, status,
        started_at, ended_at, latency_ms, output_memory_ids_json,
        decision, decision_reason, metadata_json)
        VALUES (:id, :trace_id, :parent_span_id, :user_id, :session_id,
        'memory_retrieve', 'retrieve_personal_memories', 'success',
        :started_at, :ended_at, 45,
        :output_memories, 'select_top_memories', :reason, :metadata)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": trace_root,
            "parent_span_id": s1_id,
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:30:00.050Z",
            "ended_at": "2026-06-24T18:30:00.095Z",
            "output_memories": json.dumps(["mem-001", "mem-002"]),
            "reason": "用户询问餐厅推荐，检索偏好记忆",
            "metadata": json.dumps({
                "candidate_memory_ids": ["mem-001", "mem-002", "mem-003"],
                "selected_memory_ids": ["mem-001", "mem-002"],
                "channels": {
                    "mem-001": ["key", "fts"],
                    "mem-002": ["vector"],
                },
                "scores": {"mem-001": 0.92, "mem-002": 0.78},
            }),
        },
    )

    # S3: 模型调用（意图识别）
    s3 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, parent_span_id,
        user_id, session_id, step_type, step_name, model_name, status,
        started_at, ended_at, latency_ms,
        input_event_ids_json, output_event_ids_json,
        decision, decision_reason, metadata_json)
        VALUES (:id, :trace_id, :parent_span_id, :user_id, :session_id,
        'model_call', 'parse_intent', 'deepseek-v4-flash', 'success',
        :started_at, :ended_at, 320,
        :input, :output, :decision, :reason, :metadata)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": trace_root,
            "parent_span_id": s1_id,
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:30:00.100Z",
            "ended_at": "2026-06-24T18:30:00.420Z",
            "input": json.dumps(["evt-001"]),
            "output": json.dumps(["evt-002"]),
            "decision": "call_tool",
            "reason": "用户需要实时餐厅信息，决定调用餐厅搜索工具",
            "metadata": json.dumps({
                "trigger": "requires_realtime_information",
                "selected_tool": "restaurant_search",
                "intent": "restaurant_recommendation",
                "input_tokens": 456,
                "output_tokens": 89,
            }),
        },
    )

    # E2: Tool Request
    e2 = await db.fetchone(
        """INSERT INTO events (id, user_id, session_id, seq_no, role,
        event_type, content, content_json, trace_id, created_by_span_id,
        created_at)
        VALUES (:id, :uid, :sid, 2, 'tool', 'tool_request',
        :content, :content_json, :trace, :span_id, :created_at)
        RETURNING *""",
        {
            "id": "evt-002", "uid": user_id, "sid": session_id,
            "content": "调用餐厅搜索工具",
            "content_json": json.dumps({
                "tool_name": "restaurant_search",
                "arguments": {
                    "city": "杭州",
                    "district": "滨江区",
                    "ambience": "安静",
                    "has_private_room": True,
                    "cuisine_type": "不限",
                },
            }, ensure_ascii=False),
            "trace": trace_root, "span_id": s3["id"],
            "created_at": "2026-06-24T18:30:00.430Z",
        },
    )

    # S4: Tool Call — restaurant_search
    s4 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, parent_span_id,
        user_id, session_id, step_type, step_name, tool_name,
        tool_call_id, attempt_no, status,
        started_at, ended_at, latency_ms,
        input_event_ids_json, output_event_ids_json,
        decision_reason, metadata_json)
        VALUES (:id, :trace_id, :parent_span_id, :user_id, :session_id,
        'tool_call', 'search_restaurants', 'restaurant_search',
        'tc-001', 1, 'success',
        :started_at, :ended_at, 890,
        :input, :output, :reason, :metadata)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": trace_root,
            "parent_span_id": s3["id"],
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:30:00.430Z",
            "ended_at": "2026-06-24T18:30:01.320Z",
            "input": json.dumps(["evt-002"]),
            "output": json.dumps(["evt-003"]),
            "reason": "用户需要当前餐厅信息",
            "metadata": json.dumps({
                "result_count": 6,
                "tool_version": "v2",
            }),
        },
    )

    # E3: Tool Result
    e3 = await db.fetchone(
        """INSERT INTO events (id, user_id, session_id, seq_no, role,
        event_type, content, content_json, trace_id, created_by_span_id,
        extraction_status, created_at)
        VALUES (:id, :uid, :sid, 3, 'tool', 'tool_result',
        :content, :content_json, :trace, :span_id, 'pending', :created_at)
        RETURNING *""",
        {
            "id": "evt-003", "uid": user_id, "sid": session_id,
            "content": "找到 6 家符合条件的餐厅",
            "content_json": json.dumps({
                "tool_name": "restaurant_search",
                "result_count": 6,
                "results": [
                    {"name": "西湖国宾馆·紫薇厅", "district": "西湖区", "rating": 4.8, "price": "high"},
                    {"name": "桂语山房高级餐厅", "district": "西湖区", "rating": 4.7, "price": "high"},
                    {"name": "四季酒店·金沙厅", "district": "西湖区", "rating": 4.9, "price": "high"},
                    {"name": "滨江银泰喜来登·采悦轩", "district": "滨江区", "rating": 4.5, "price": "mid"},
                    {"name": "龙井草堂", "district": "西湖区", "rating": 4.6, "price": "high"},
                    {"name": "外婆家·杭小馆", "district": "滨江区", "rating": 4.3, "price": "low"},
                ],
            }, ensure_ascii=False),
            "trace": trace_root, "span_id": s4["id"],
            "created_at": "2026-06-24T18:30:01.320Z",
        },
    )

    # S5: Generate Response
    s5 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, parent_span_id,
        user_id, session_id, step_type, step_name, model_name, status,
        started_at, ended_at, latency_ms,
        input_event_ids_json, input_memory_ids_json,
        output_event_ids_json, metadata_json)
        VALUES (:id, :trace_id, :parent_span_id, :user_id, :session_id,
        'response', 'generate_final_response', 'deepseek-v4-flash', 'success',
        :started_at, :ended_at, 1250,
        :input_events, :input_memories, :output, :metadata)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": trace_root,
            "parent_span_id": s1_id,
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:30:01.330Z",
            "ended_at": "2026-06-24T18:30:02.580Z",
            "input_events": json.dumps(["evt-001", "evt-003"]),
            "input_memories": json.dumps(["mem-001", "mem-002"]),
            "output": json.dumps(["evt-004"]),
            "metadata": json.dumps({
                "provided_memory_ids": ["mem-001", "mem-002"],
                "used_memory_ids": ["mem-001"],
                "provided_tool_result_ids": ["evt-003"],
                "used_tool_result_ids": ["evt-003"],
            }),
        },
    )

    # E4: Assistant Response
    e4 = await db.fetchone(
        """INSERT INTO events (id, user_id, session_id, seq_no, role,
        event_type, content, content_json, trace_id, created_by_span_id,
        extraction_status, created_at)
        VALUES (:id, :uid, :sid, 4, 'assistant', 'assistant_message',
        :content, :content_json, :trace, :span_id, 'pending', :created_at)
        RETURNING *""",
        {
            "id": "evt-004", "uid": user_id, "sid": session_id,
            "content": (
                "根据您的需求，我为您推荐以下几家适合请客户吃饭的餐厅：\n\n"
                "1. **四季酒店·金沙厅** ⭐4.9 — 西湖区，高端环境，有包间\n"
                "2. **西湖国宾馆·紫薇厅** ⭐4.8 — 西湖区，环境安静雅致\n"
                "3. **桂语山房高级餐厅** ⭐4.7 — 西湖区，氛围很好\n"
                "4. **滨江银泰喜来登·采悦轩** ⭐4.5 — 滨江区，离您较近\n\n"
                "您之前在滨江区，这几家餐厅都适合商务用餐。需要我帮您预订吗？"
            ),
            "content_json": json.dumps({
                "recommendations": [
                    "四季酒店·金沙厅", "西湖国宾馆·紫薇厅",
                    "桂语山房高级餐厅", "滨江银泰喜来登·采悦轩",
                ],
                "model": "deepseek-v4-flash",
                "response_type": "restaurant_recommendation",
            }, ensure_ascii=False),
            "trace": trace_root, "span_id": s5["id"],
            "created_at": "2026-06-24T18:30:02.580Z",
        },
    )

    # ================================================================
    # Turn 2 — 用户提供新信息
    # ================================================================

    t2_trace = new_uuid()
    s6 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, user_id, session_id,
        step_type, step_name, status, started_at, ended_at, latency_ms)
        VALUES (:id, :trace_id, :user_id, :session_id,
        'input', 'receive_user_message', 'success',
        :started_at, :ended_at, 10)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": t2_trace,
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:32:00.000Z",
            "ended_at": "2026-06-24T18:32:00.010Z",
        },
    )

    e5 = await db.fetchone(
        """INSERT INTO events (id, user_id, session_id, seq_no, role,
        event_type, content, trace_id, created_by_span_id, created_at)
        VALUES (:id, :uid, :sid, 5, 'user', 'user_message',
        :content, :trace, :span_id, :created_at)
        RETURNING *""",
        {
            "id": "evt-005", "uid": user_id, "sid": session_id,
            "content": "对了，我最近搬到上海了，不在杭州了。而且张总他不能吃辣。",
            "trace": t2_trace, "span_id": s6["id"],
            "created_at": "2026-06-24T18:32:00.000Z",
        },
    )

    # S7: 模型调用 — 提取记忆
    s7 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, parent_span_id,
        user_id, session_id, step_type, step_name, model_name, status,
        started_at, ended_at, latency_ms,
        output_event_ids_json, decision_reason, metadata_json)
        VALUES (:id, :trace_id, :parent_span_id, :user_id, :session_id,
        'memory_extract', 'extract_memory_from_message',
        'deepseek-v4-flash', 'success',
        :started_at, :ended_at, 450,
        :output, :reason, :metadata)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": t2_trace,
            "parent_span_id": s6["id"],
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:32:00.020Z",
            "ended_at": "2026-06-24T18:32:00.470Z",
            "output": json.dumps(["evt-006"]),
            "reason": "用户明确表达了长期事实变更",
            "metadata": json.dumps({
                "extracted_facts": ["residence.city", "food.allergy.zhang"],
                "input_tokens": 312,
                "output_tokens": 67,
            }),
        },
    )

    # E6: 模型提取的候选记忆输出（JSON）
    e6 = await db.fetchone(
        """INSERT INTO events (id, user_id, session_id, seq_no, role,
        event_type, content, content_json, trace_id, created_by_span_id,
        extraction_status, created_at)
        VALUES (:id, :uid, :sid, 6, 'system', 'memory_candidate',
        :content, :content_json, :trace, :span_id, 'pending', :created_at)
        RETURNING *""",
        {
            "id": "evt-006", "uid": user_id, "sid": session_id,
            "content": "记忆提取候选",
            "content_json": json.dumps({
                "memories": [
                    {
                        "memory_type": "fact",
                        "memory_key": "residence.city",
                        "content": "用户目前居住在上海",
                        "value_json": {"city": "上海", "previous_city": "杭州"},
                        "importance": 0.85,
                        "confidence": 0.95,
                        "source_event_ids": ["evt-005"],
                    },
                    {
                        "memory_type": "fact",
                        "memory_key": "food.allergy.zhang",
                        "content": "张总不能吃辣",
                        "value_json": {"person": "张总", "restriction": "不能吃辣"},
                        "importance": 0.7,
                        "confidence": 0.9,
                        "source_event_ids": ["evt-005"],
                    },
                ],
            }, ensure_ascii=False),
            "trace": t2_trace, "span_id": s7["id"],
            "created_at": "2026-06-24T18:32:00.470Z",
        },
    )

    # 写入记忆 M1: residence.city (新替代旧)
    m1 = await db.fetchone(
        """INSERT INTO memories (id, user_id, memory_type, memory_key,
        content, value_json, importance, confidence,
        source_event_ids_json, supersedes_id,
        created_by_span_id, updated_by_span_id,
        created_at, updated_at)
        VALUES (:id, :uid, 'fact', 'residence.city',
        :content, :value_json, 0.85, 0.95,
        :source_ids, NULL,
        :span_id, :span_id, :created_at, :updated_at)
        RETURNING *""",
        {
            "id": "mem-001", "uid": user_id,
            "content": "用户目前居住在上海",
            "value_json": json.dumps({"city": "上海"}, ensure_ascii=False),
            "source_ids": json.dumps(["evt-005"]),
            "span_id": s7["id"],
            "created_at": "2026-06-24T18:32:00.500Z",
            "updated_at": "2026-06-24T18:32:00.500Z",
        },
    )

    # M2: preference
    m2 = await db.fetchone(
        """INSERT INTO memories (id, user_id, memory_type, memory_key,
        content, value_json, importance, confidence,
        source_event_ids_json,
        created_by_span_id, updated_by_span_id,
        created_at, updated_at)
        VALUES (:id, :uid, 'preference', 'restaurant.ambience',
        :content, :value_json, 0.75, 0.9,
        :source_ids, :span_id, :span_id, :created_at, :updated_at)
        RETURNING *""",
        {
            "id": "mem-002", "uid": user_id,
            "content": "用户偏好安静、适合商务宴请的餐厅环境",
            "value_json": json.dumps({
                "ambience": "安静", "purpose": "商务宴请", "features": ["包间"],
            }, ensure_ascii=False),
            "source_ids": json.dumps(["evt-001", "evt-004"]),
            "span_id": s7["id"],
            "created_at": "2026-06-24T18:32:00.500Z",
            "updated_at": "2026-06-24T18:32:00.500Z",
        },
    )

    # M3: 关于张总的记忆
    m3 = await db.fetchone(
        """INSERT INTO memories (id, user_id, memory_type, memory_key,
        content, value_json, importance, confidence,
        source_event_ids_json,
        created_by_span_id, updated_by_span_id,
        created_at, updated_at)
        VALUES (:id, :uid, 'fact', 'food.allergy.zhang',
        :content, :value_json, 0.7, 0.9,
        :source_ids, :span_id, :span_id, :created_at, :updated_at)
        RETURNING *""",
        {
            "id": "mem-003", "uid": user_id,
            "content": "张总不能吃辣",
            "value_json": json.dumps({
                "person": "张总", "restriction": "不能吃辣",
            }, ensure_ascii=False),
            "source_ids": json.dumps(["evt-005"]),
            "span_id": s7["id"],
            "created_at": "2026-06-24T18:32:00.500Z",
            "updated_at": "2026-06-24T18:32:00.500Z",
        },
    )

    # ================================================================
    # Turn 3 — 用户继续话题，基于新信息
    # ================================================================

    t3_trace = new_uuid()
    s8 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, user_id, session_id,
        step_type, step_name, status, started_at, ended_at, latency_ms)
        VALUES (:id, :trace_id, :user_id, :session_id,
        'input', 'receive_user_message', 'success',
        :started_at, :ended_at, 8)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": t3_trace,
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:33:00.000Z",
            "ended_at": "2026-06-24T18:33:00.008Z",
        },
    )

    e7 = await db.fetchone(
        """INSERT INTO events (id, user_id, session_id, seq_no, role,
        event_type, content, trace_id, created_by_span_id, created_at)
        VALUES (:id, :uid, :sid, 7, 'user', 'user_message',
        :content, :trace, :span_id, :created_at)
        RETURNING *""",
        {
            "id": "evt-007", "uid": user_id, "sid": session_id,
            "content": "那现在在上海有什么适合和张总吃饭的餐厅推荐？别太辣的。",
            "trace": t3_trace, "span_id": s8["id"],
            "created_at": "2026-06-24T18:33:00.000Z",
        },
    )

    # S9: 记忆检索
    s9 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, parent_span_id,
        user_id, session_id, step_type, step_name, status,
        started_at, ended_at, latency_ms,
        output_memory_ids_json, metadata_json)
        VALUES (:id, :trace_id, :parent_span_id, :user_id, :session_id,
        'memory_retrieve', 'retrieve_personal_memories', 'success',
        :started_at, :ended_at, 38,
        :output_memories, :metadata)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": t3_trace,
            "parent_span_id": s8["id"],
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:33:00.010Z",
            "ended_at": "2026-06-24T18:33:00.048Z",
            "output_memories": json.dumps(["mem-001", "mem-002", "mem-003"]),
            "metadata": json.dumps({
                "candidate_memory_ids": ["mem-001", "mem-002", "mem-003"],
                "selected_memory_ids": ["mem-001", "mem-002", "mem-003"],
                "channels": {
                    "mem-001": ["key"],
                    "mem-002": ["fts", "vector"],
                    "mem-003": ["fts"],
                },
            }),
        },
    )

    # S10: Model call — 生成回答 (with memory context)
    s10 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, parent_span_id,
        user_id, session_id, step_type, step_name, model_name, status,
        started_at, ended_at, latency_ms,
        input_event_ids_json, input_memory_ids_json,
        output_event_ids_json, metadata_json)
        VALUES (:id, :trace_id, :parent_span_id, :user_id, :session_id,
        'response', 'generate_final_response', 'deepseek-v4-flash', 'success',
        :started_at, :ended_at, 980,
        :input_events, :input_memories, :output, :metadata)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": t3_trace,
            "parent_span_id": s8["id"],
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:33:00.050Z",
            "ended_at": "2026-06-24T18:33:01.030Z",
            "input_events": json.dumps(["evt-007"]),
            "input_memories": json.dumps(["mem-001", "mem-002", "mem-003"]),
            "output": json.dumps(["evt-008"]),
            "metadata": json.dumps({
                "provided_memory_ids": ["mem-001", "mem-002", "mem-003"],
                "used_memory_ids": ["mem-001", "mem-002", "mem-003"],
            }),
        },
    )

    e8 = await db.fetchone(
        """INSERT INTO events (id, user_id, session_id, seq_no, role,
        event_type, content, content_json, trace_id, created_by_span_id,
        extraction_status, created_at)
        VALUES (:id, :uid, :sid, 8, 'assistant', 'assistant_message',
        :content, :content_json, :trace, :span_id, 'pending', :created_at)
        RETURNING *""",
        {
            "id": "evt-008", "uid": user_id, "sid": session_id,
            "content": (
                "好的，我已经知道您搬到上海了，而且张总不能吃辣。"
                "为您推荐以下上海适合商务宴请的餐厅：\n\n"
                "1. **福和慧** ⭐4.9 — 长宁区，高端素食，环境雅致，有包间\n"
                "2. **新荣记（南阳路店）** ⭐4.8 — 静安区，粤菜为主，不辣\n"
                "3. **甬府** ⭐4.7 — 黄浦区，宁波菜，口味清淡\n"
                "4. **菁禧荟** ⭐4.7 — 长宁区，潮州菜，适合商务\n"
                "5. **莱美露滋** ⭐4.9 — 浦东新区，法餐，环境顶级\n\n"
                "这几家都不辣，环境适合商务宴请。需要帮您预订吗？"
            ),
            "content_json": json.dumps({
                "recommendations": [
                    "福和慧", "新荣记（南阳路店）",
                    "甬府", "菁禧荟", "莱美露滋",
                ],
                "based_on_memories": ["mem-001", "mem-002", "mem-003"],
                "model": "deepseek-v4-flash",
            }, ensure_ascii=False),
            "trace": t3_trace, "span_id": s10["id"],
            "created_at": "2026-06-24T18:33:01.030Z",
        },
    )

    # ================================================================
    # 后台提取 Trace (T200) — 模拟对话结束后的记忆提取
    # ================================================================

    ext_trace = new_uuid()
    ext_group = "ext-grp-001"

    # Claim 提取组 (events 1-8, 标记为 processing)
    await db.execute(
        """UPDATE events SET extraction_status = 'processing',
        extraction_group_id = :gid, extraction_attempts = 1
        WHERE user_id = :uid AND session_id = :sid
        AND seq_no BETWEEN 1 AND 8""",
        {"gid": ext_group, "uid": user_id, "sid": session_id},
    )

    s_ext1 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, user_id, session_id,
        step_type, step_name, status, started_at, ended_at, latency_ms,
        output_event_ids_json, decision_reason)
        VALUES (:id, :trace_id, :user_id, :session_id,
        'memory_extract', 'create_extraction_group', 'success',
        :started_at, :ended_at, 5,
        :output, '分配提取组')
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": ext_trace,
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:43:00.000Z",
            "ended_at": "2026-06-24T18:43:00.005Z",
            "output": json.dumps(["evt-001", "evt-002", "evt-003",
                                  "evt-004", "evt-005", "evt-006",
                                  "evt-007", "evt-008"]),
        },
    )

    s_ext2 = await db.fetchone(
        """INSERT INTO trace_events (id, trace_id, parent_span_id,
        user_id, session_id, step_type, step_name, model_name, status,
        started_at, ended_at, latency_ms,
        decision_reason, metadata_json)
        VALUES (:id, :trace_id, :parent_span_id, :user_id, :session_id,
        'memory_extract', 'extract_memory_candidates',
        'deepseek-v4-flash', 'success',
        :started_at, :ended_at, 1200,
        :reason, :metadata)
        RETURNING *""",
        {
            "id": new_uuid(), "trace_id": ext_trace,
            "parent_span_id": s_ext1["id"],
            "user_id": user_id, "session_id": session_id,
            "started_at": "2026-06-24T18:43:00.010Z",
            "ended_at": "2026-06-24T18:43:01.210Z",
            "reason": "从8条对话事件中提取记忆",
            "metadata": json.dumps({
                "input_tokens": 2340,
                "output_tokens": 180,
                "extracted_count": 2,
            }),
        },
    )

    # 提取完成 — 标记事件 done + 写入所有记忆
    await db.execute(
        """UPDATE events SET extraction_status = 'done',
        extracted_at = '2026-06-24T18:43:01.250Z'
        WHERE extraction_group_id = :gid""",
        {"gid": ext_group},
    )

    # 增加一条系统规则记忆
    m4 = await db.fetchone(
        """INSERT INTO memories (id, user_id, memory_type, memory_key,
        content, value_json, importance, confidence,
        source_group_id, source_event_ids_json, created_by_span_id,
        updated_by_span_id, created_at, updated_at)
        VALUES (:id, :uid, 'rule', 'booking.confirmation',
        :content, :value_json, 0.9, 1.0,
        :group_id, :source_ids, :span_id, :span_id,
        :created_at, :updated_at)
        RETURNING *""",
        {
            "id": "mem-004", "uid": user_id,
            "content": "在帮助用户预订之前，必须先征得用户的明确确认和同意",
            "value_json": json.dumps({
                "rule": "booking_confirmation",
                "scope": "all_bookings",
            }, ensure_ascii=False),
            "group_id": ext_group,
            "source_ids": json.dumps(["evt-004", "evt-008"]),
            "span_id": s_ext2["id"],
            "created_at": "2026-06-24T18:43:01.250Z",
            "updated_at": "2026-06-24T18:43:01.250Z",
        },
    )

    # M5: event 类型记忆
    m5 = await db.fetchone(
        """INSERT INTO memories (id, user_id, memory_type, memory_key,
        content, value_json, importance, confidence,
        source_group_id, source_event_ids_json, created_by_span_id,
        updated_by_span_id, created_at, updated_at)
        VALUES (:id, :uid, 'event', 'event.restaurant_feedback.2026-06-24',
        :content, :value_json, 0.5, 0.85,
        :group_id, :source_ids, :span_id, :span_id,
        :created_at, :updated_at)
        RETURNING *""",
        {
            "id": "mem-005", "uid": user_id,
            "content": "用户曾询问杭州餐厅推荐，后告知已搬到上海，并提到张总不能吃辣",
            "value_json": json.dumps({
                "date": "2026-06-24",
                "event_type": "restaurant_recommendation",
                "location_change": "杭州→上海",
                "involved_person": "张总",
            }, ensure_ascii=False),
            "group_id": ext_group,
            "source_ids": json.dumps(["evt-001", "evt-004", "evt-005", "evt-008"]),
            "span_id": s_ext2["id"],
            "created_at": "2026-06-24T18:43:01.250Z",
            "updated_at": "2026-06-24T18:43:01.250Z",
        },
    )

    # ================================================================
    # 导出 JSON
    # ================================================================
    trace_events = await db.fetchall(
        "SELECT * FROM trace_events ORDER BY started_at, created_at"
    )
    events = await db.fetchall(
        "SELECT * FROM events ORDER BY session_id, seq_no"
    )
    memories = await db.fetchall(
        "SELECT * FROM memories ORDER BY memory_type, memory_key"
    )

    # 替换 BLOB 为描述（向量BLOB不可JSON序列化）
    for m in memories:
        if m.get("embedding") is not None:
            m["embedding"] = f"<BLOB len={len(m['embedding'])} bytes>"

    export = {
        "database": str(db_path),
        "user_id": user_id,
        "session_id": session_id,
        "trace_count": 4,
        "summary": {
            "trace_events": len(trace_events),
            "events": len(events),
            "memories": len(memories),
        },
        "trace_events": trace_events,
        "events": events,
        "memories": memories,
    }

    out_path = os.path.join(
        os.path.dirname(__file__) or ".",
        "demo_session_export.json",
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2, default=str)

    print(f"✅ 数据库已写入: {db_path}")
    print(f"   trace_events: {len(trace_events)} 行")
    print(f"   events:       {len(events)} 行")
    print(f"   memories:     {len(memories)} 行")
    print(f"✅ JSON 已导出:  {out_path}")
    print(f"   文件大小: {os.path.getsize(out_path) / 1024:.1f} KB")

    await db.close()


asyncio.run(main())
