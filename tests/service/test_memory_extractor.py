"""Tests for MemoryExtractor — 自动候选提取。

覆盖场景：
- 少于阈值消息数时不提取
- 无 router 时跳过
- 空 principal_id 时跳过
- JSON 解析正常
- 显式用户陈述写入 confirmed
- 推断写入 candidate
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.domain.memory import MemoryKind, MemoryStatus
from cogito.service.memory_extractor import MemoryExtractor
from cogito.service.memory_service import SqliteMemoryService
from cogito.store.migration import migrate
from cogito.store.time_utils import epoch_ms


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def service(db) -> SqliteMemoryService:
    return SqliteMemoryService(db)


class TestMemoryExtractor:
    def test_no_router_skips_extraction(self, db, service):
        extractor = MemoryExtractor(db, service, router=None)
        result = extractor.extract_from_messages(
            [{"role": "user", "content": "Hi"}], "p1",
        )
        # 这是协程，需要 await
        import asyncio
        items = asyncio.run(result)
        assert items == []

    def test_few_messages_skips_extraction(self, db, service):
        """少于阈值时不提取。"""
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider()
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        # 只有 2 条消息，不足 4 条
        result = extractor.extract_from_messages(
            [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ],
            "p1",
        )
        import asyncio
        items = asyncio.run(result)
        assert items == []

    def test_empty_principal_skips(self, db, service):
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider

        provider = StubModelProvider()
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        result = extractor.extract_from_messages(
            [{"role": "user", "content": "Hi"}] * 5, "",
        )
        import asyncio
        items = asyncio.run(result)
        assert items == []

    def test_parse_json_success(self, db, service):
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        # Stub 返回 JSON
        provider = StubModelProvider(scenarios=[
            StubScenario(
                response_text='{"candidates": [{"kind": "preference", "subject": "user", '
                              '"predicate": "lang", "value": "Python", '
                              '"explicitness": "explicit_user_statement", '
                              '"confidence": 0.95, "importance": 0.8}]}',
            ),
        ])
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        result = extractor.extract_from_messages(
            [{"role": "user", "content": "I like Python"}] * 5, "p1",
        )
        import asyncio
        items = asyncio.run(result)
        assert len(items) == 1
        assert items[0]["kind"] == "preference"
        assert items[0]["value"] == "Python"

        # 显式陈述 → confirmed
        mem = service.retrieve(principal_id="p1", query="Python")
        assert len(mem) > 0
        assert mem[0].status == MemoryStatus.confirmed

    def test_inference_writes_candidate(self, db, service):
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(scenarios=[
            StubScenario(
                response_text='{"candidates": [{"kind": "preference", "subject": "user", '
                              '"predicate": "style", "value": "concise", '
                              '"explicitness": "model_inference", '
                              '"confidence": 0.6, "importance": 0.4}]}',
            ),
        ])
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        result = extractor.extract_from_messages(
            [{"role": "user", "content": "I write concise code"}] * 5, "p1",
        )
        import asyncio
        items = asyncio.run(result)
        assert len(items) == 1

        # 推断 → candidate（candidate 不会被 retrieve 返回）
        row = db.execute(
            "SELECT status, value FROM memory_items WHERE value='concise'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "candidate"

    def test_parse_failure_returns_empty(self, db, service):
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(scenarios=[
            StubScenario(response_text="I don't see anything to extract here."),
        ])
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        result = extractor.extract_from_messages(
            [{"role": "user", "content": "Hello"}] * 5, "p1",
        )
        import asyncio
        items = asyncio.run(result)
        assert items == []

    def test_format_messages(self, db, service):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        formatted = MemoryExtractor._format_messages(msgs)
        assert "[user]: Hello" in formatted
        assert "[assistant]: Hi there" in formatted
