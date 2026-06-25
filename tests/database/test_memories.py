"""
Tests for cogito.database memories — MemoryRepository, MemoryWriter, MemoryRetriever

对应设计文档第 11 节、第 12 节、第 14 节、第 15 节、第 16 节、第 17 节。
"""

from __future__ import annotations

import json
import struct

import pytest

from cogito.database.ids import new_uuid
from cogito.database.repository.memories import MemoryRepository
from cogito.database.service.memory_retriever import MemoryRetriever
from cogito.database.service.memory_writer import MemoryWriter


USER_ID = "test-user"
SPAN_ID = new_uuid()
NOW = "2026-06-24T12:00:00.000Z"


@pytest.fixture
def event_ids():
    return [new_uuid() for _ in range(3)]


class TestMemoryRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, db):
        repo = MemoryRepository(db)
        mid = new_uuid()
        row = await repo.insert({
            "id": mid,
            "user_id": USER_ID,
            "memory_type": "fact",
            "memory_key": "test.key",
            "content": "test content",
        })
        assert row["id"] == mid
        assert row["status"] == "active"

        found = await repo.get_by_id(mid)
        assert found["content"] == "test content"

    @pytest.mark.asyncio
    async def test_get_active_by_key(self, db):
        repo = MemoryRepository(db)
        key = "city"
        await repo.insert({
            "user_id": USER_ID,
            "memory_type": "fact",
            "memory_key": key,
            "content": "city content",
        })
        mem = await repo.get_active_by_key(USER_ID, key)
        assert mem is not None
        assert mem["memory_key"] == key

    @pytest.mark.asyncio
    async def test_unique_active_key(self, db):
        repo = MemoryRepository(db)
        key = "unique.key"
        await repo.insert({
            "user_id": USER_ID,
            "memory_type": "fact",
            "memory_key": key,
            "content": "first",
        })
        with pytest.raises(Exception):
            await repo.insert({
                "user_id": USER_ID,
                "memory_type": "fact",
                "memory_key": key,
                "content": "second",
            })

    @pytest.mark.asyncio
    async def test_fts_search(self, db):
        repo = MemoryRepository(db)
        await repo.insert({
            "user_id": USER_ID,
            "memory_type": "fact",
            "memory_key": "food.prefer",
            "content": "用户喜欢吃四川菜和湖南菜",
        })
        # trigram 需要至少 3 个字符，使用完整词组或 3+ 字符查询
        results = await repo.search_fts(USER_ID, "湖南菜", NOW)
        assert len(results) >= 1
        assert "湖南菜" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_like_fallback(self, db):
        repo = MemoryRepository(db)
        await repo.insert({
            "user_id": USER_ID,
            "memory_type": "fact",
            "memory_key": "short.kw",
            "content": "杭州是个好城市",
        })
        results = await repo.search_like(USER_ID, "杭州")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_source_events(self, db, event_ids):
        repo = MemoryRepository(db)
        source_json = json.dumps(event_ids, ensure_ascii=False)
        mid = new_uuid()
        await repo.insert({
            "id": mid,
            "user_id": USER_ID,
            "memory_type": "fact",
            "memory_key": "source.test",
            "content": "source test",
            "source_event_ids_json": source_json,
        })
        # Event table has no rows matching our source ids, so no results
        # Just verify no error
        results = await repo.get_source_events(mid)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_increment_access_count(self, db):
        repo = MemoryRepository(db)
        mid = new_uuid()
        await repo.insert({
            "id": mid,
            "user_id": USER_ID,
            "memory_type": "fact",
            "memory_key": "access.test",
            "content": "access test",
        })
        await repo.increment_access_count(mid, NOW)
        mem = await repo.get_by_id(mid)
        assert mem["access_count"] >= 1

    @pytest.mark.asyncio
    async def test_delete_old_traces(self, db):
        repo = MemoryRepository(db)
        mid = new_uuid()
        await repo.insert({
            "id": mid,
            "user_id": USER_ID,
            "memory_type": "fact",
            "memory_key": "delete.test",
            "content": "to delete",
        })
        deleted = await repo.hard_delete(mid)
        assert deleted is True
        assert await repo.get_by_id(mid) is None


class TestMemoryWriter:
    @pytest.mark.asyncio
    async def test_create_new_memory(self, db):
        writer = MemoryWriter(db)
        # 先创建 trace_event 以满足外键约束
        span_id = new_uuid()
        await db.execute(
            "INSERT INTO trace_events (id, trace_id, user_id, step_type, step_name) "
            "VALUES (:id, :tid, :uid, 'test', 'writer_test')",
            {"id": span_id, "tid": new_uuid(), "uid": USER_ID},
        )
        mem = await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="residence.city",
            content="用户目前居住在杭州",
            value_json={"city": "杭州"},
            importance=0.8,
            confidence=0.95,
            created_by_span_id=span_id,
        )
        assert mem["status"] == "active"
        assert mem["memory_key"] == "residence.city"

    @pytest.mark.asyncio
    async def test_reinforce_memory(self, db):
        writer = MemoryWriter(db)
        key = "pref.temp"
        # First write
        await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="preference",
            memory_key=key,
            content="用户喜欢安静的环境",
            confidence=0.8,
        )
        # Second write (same content)
        reinforced = await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="preference",
            memory_key=key,
            content="用户喜欢安静的环境",
            confidence=0.8,
        )
        assert reinforced["confidence"] > 0.8  # boosted

    @pytest.mark.asyncio
    async def test_supersede_memory(self, db):
        writer = MemoryWriter(db)
        key = "residence.city"
        # First: 杭州
        old = await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key=key,
            content="用户目前居住在杭州",
            value_json={"city": "杭州"},
            importance=0.8,
        )
        # Second: 上海 (different content → supersede)
        new = await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key=key,
            content="用户目前居住在上海",
            value_json={"city": "上海"},
            importance=0.8,
        )
        assert new["supersedes_id"] == old["id"]

        # Old should be superseded
        old_mem = await writer._repo.get_by_id(old["id"])
        assert old_mem["status"] == "superseded"

    @pytest.mark.asyncio
    async def test_invalid_memory_type(self, db):
        writer = MemoryWriter(db)
        with pytest.raises(ValueError):
            await writer.upsert_memory(
                user_id=USER_ID,
                memory_type="invalid_type",
                memory_key="test",
                content="test",
            )

    @pytest.mark.asyncio
    async def test_complete_extraction(self, db):
        writer = MemoryWriter(db)
        group_id = new_uuid()

        # 先创建 trace_event 以满足外键约束
        span_id = new_uuid()
        await db.execute(
            "INSERT INTO trace_events (id, trace_id, user_id, step_type, step_name) "
            "VALUES (:id, :tid, :uid, 'test', 'extraction')",
            {"id": span_id, "tid": new_uuid(), "uid": USER_ID},
        )

        # Create extraction events
        events_repo = writer._event_service._repo
        for i in range(1, 3):
            await events_repo.insert({
                "user_id": USER_ID,
                "session_id": "s10",
                "seq_no": i,
                "role": "user",
                "event_type": "user_message",
                "content": f"msg_{i}",
            })
        await events_repo.claim_extraction(USER_ID, "s10", 1, 2, group_id)

        # Complete extraction
        memories = await writer.complete_extraction(
            group_id,
            memories=[
                {
                    "user_id": USER_ID,
                    "memory_type": "fact",
                    "memory_key": "extracted.key",
                    "content": "Extracted memory",
                    "value_json": {"key": "value"},
                    "importance": 0.7,
                    "confidence": 0.9,
                    "source_event_ids": ["evt-1"],
                },
            ],
            created_by_span_id=span_id,
        )
        assert len(memories) == 1
        assert memories[0]["memory_key"] == "extracted.key"


class TestMemoryRetriever:
    @pytest.mark.asyncio
    async def test_retrieve_by_key(self, db):
        retriever = MemoryRetriever(db)
        writer = MemoryWriter(db)
        await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="test.k1",
            content="key query test",
        )
        mem = await retriever.retrieve_by_key(USER_ID, "test.k1")
        assert mem is not None
        assert mem["content"] == "key query test"

    @pytest.mark.asyncio
    async def test_keyword_search(self, db):
        retriever = MemoryRetriever(db)
        writer = MemoryWriter(db)
        await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="fts.test",
            content="用户喜欢打篮球和游泳",
        )
        msg = "keyword_search via class method"
        results = await retriever.keyword_search("篮球", user_id=USER_ID)
        assert len(results) >= 1, msg

        # 独立函数方式
        from cogito.database.service.memory_retriever import keyword_search
        results2 = await keyword_search(db, USER_ID, "篮球")
        assert len(results2) >= 1

    @pytest.mark.asyncio
    async def test_short_keyword_like_fallback(self, db):
        retriever = MemoryRetriever(db)
        writer = MemoryWriter(db)
        await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="like.test",
            content="杭州西湖",
        )
        # "杭州" is 2 chars, which is < 3, so it should use LIKE fallback
        results = await retriever.keyword_search("杭州", user_id=USER_ID)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_hybrid_search(self, db):
        retriever = MemoryRetriever(db)
        writer = MemoryWriter(db)
        await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="hybrid.city",
            content="用户目前居住在上海",
            value_json={"city": "上海"},
            importance=0.9,
        )
        await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="preference",
            memory_key="hybrid.food",
            content="用户喜欢吃辣的川菜",
            importance=0.7,
        )
        results = await retriever.hybrid_search(
            user_id=USER_ID,
            keywords=["上海", "川菜"],
            top_k=5,
        )
        assert len(results) >= 2

    @pytest.mark.asyncio
    async def test_hybrid_search_empty(self, db):
        retriever = MemoryRetriever(db)
        results = await retriever.hybrid_search(
            user_id="nonexistent-user",
            keywords=["nothing"],
            top_k=5,
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_vector_search_function(self, db):
        """测试 vector_search 独立函数。"""
        from cogito.database.service.memory_retriever import (
            serialize_embedding,
            vector_search,
        )
        import struct

        writer = MemoryWriter(db)
        emb = [0.1, 0.2, 0.3, 0.4]
        emb_blob = serialize_embedding(emb)

        await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="fact",
            memory_key="vector.test",
            content="向量测试记忆",
            embedding=emb_blob,
            embedding_dim=4,
            embedding_model="test",
        )

        results = await vector_search(db, USER_ID, [0.1, 0.2, 0.3, 0.4], top_k=5)
        assert len(results) >= 1
        assert "similarity" in results[0]
        assert results[0]["similarity"] > 0.99

    @pytest.mark.asyncio
    async def test_retrieve_by_type(self, db):
        retriever = MemoryRetriever(db)
        writer = MemoryWriter(db)
        await writer.upsert_memory(
            user_id=USER_ID,
            memory_type="rule",
            memory_key="rule.test",
            content="必须先确认再付款",
            importance=0.9,
        )
        results = await retriever.retrieve_by_type(USER_ID, "rule")
        assert len(results) >= 1
