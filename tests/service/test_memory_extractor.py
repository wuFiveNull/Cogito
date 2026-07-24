"""Tests for MemoryExtractor — 自动候选提取（PLAN-13 P13-02: 精确 evidence）。

覆盖场景：
- 少于阈值消息数时不提取
- 无 router 时跳过
- 空 principal_id 时跳过
- JSON 解析正常
- 显式用户陈述写入 confirmed
- 推断写入 candidate
- 精确来源追溯（evidence_message_ids → memory_sources）
- 模型返回窗口外 evidence ID 被过滤
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from cogito.domain.memory import MemoryStatus
from cogito.service.memory_extractor import (
    ExtractMessage,
    MemoryExtractor,
    MemoryExtractionWriteError,
)
from cogito.service.memory_service import SqliteMemoryService
from cogito.store.migration import migrate


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


def _msgs(contents: list[str]) -> list[ExtractMessage]:
    """快速构建 ExtractMessage 列表（含唯一 message_id）。"""
    return [
        ExtractMessage(
            message_id=f"msg_{i}",
            role="user" if i % 2 == 0 else "assistant",
            content=c,
            receive_sequence=i,
            sender_principal_id="p1",
        )
        for i, c in enumerate(contents)
    ]


class TestMemoryExtractor:
    def test_locked_candidate_write_emits_safe_diagnostics(self, db, service, caplog, monkeypatch):
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(
            scenarios=[
                StubScenario(
                    response_text=(
                        '{"candidates": [{"kind": "fact", "subject": "user", '
                        '"predicate": "secret", "value": "do-not-log", '
                        '"evidence_message_ids": ["msg_0"]}]}'
                    )
                )
            ]
        )
        extractor = MemoryExtractor(
            db,
            service,
            router=ModelRouter(
                providers={"extractor": provider},
                role_map={"memory_extractor": "extractor"},
            ),
            strict=True,
        )

        def _locked_write(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(extractor, "_write_candidate", _locked_write)

        with pytest.raises(MemoryExtractionWriteError):
            asyncio.run(
                extractor.extract_from_messages(
                    _msgs(["message"] * 5),
                    "p1",
                    session_id="s1",
                )
            )

        assert "Memory candidate write locked" in caplog.text
        assert "candidate_index=1" in caplog.text
        assert "do-not-log" not in caplog.text

    def test_invalid_json_schema_retries_with_json_object(self, db, service):
        from cogito.model.contracts import ErrorCategory, ErrorEnvelope
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(
            scenarios=[
                StubScenario(
                    error=ErrorEnvelope(
                        category=ErrorCategory.invalid_request,
                        message="JSON Schema unsupported",
                        retryable=False,
                    )
                ),
                StubScenario(response_text='{"candidates": []}'),
            ]
        )
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        items = asyncio.run(
            extractor.extract_from_messages(
                _msgs(["Remember this"] * 5),
                "p1",
                session_id="s1",
            )
        )

        assert items == []
        assert len(provider.received_requests) == 2
        assert provider.received_requests[0].response_schema is not None
        assert provider.received_requests[1].response_schema is None
        assert provider.received_requests[1].response_format == "json"

    def test_no_router_skips_extraction(self, db, service):
        extractor = MemoryExtractor(db, service, router=None)
        items = asyncio.run(
            extractor.extract_from_messages(
                _msgs(["Hi"]),
                "p1",
                session_id="s1",
            )
        )
        assert items == []

    def test_few_messages_skips_extraction(self, db, service):
        """少于阈值时不提取。"""
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider

        provider = StubModelProvider()
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        # 只有 2 条消息，不足 4 条
        items = asyncio.run(
            extractor.extract_from_messages(
                _msgs(["Hi", "Hello"]),
                "p1",
                session_id="s1",
            )
        )
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

        items = asyncio.run(
            extractor.extract_from_messages(
                _msgs(["Hi"] * 5),
                "",
                session_id="s1",
            )
        )
        assert items == []

    def test_parse_json_success(self, db, service):
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(
            scenarios=[
                StubScenario(
                    response_text='{"candidates": [{"kind": "preference", '
                    '"subject": "user", "predicate": "lang", '
                    '"value": "Python", '
                    '"explicitness": "explicit_user_statement", '
                    '"confidence": 0.95, "importance": 0.8, '
                    '"evidence_message_ids": ["msg_0", "msg_2"]}]}',
                ),
            ]
        )
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        msgs = _msgs(["I like Python"] * 5)
        items = asyncio.run(
            extractor.extract_from_messages(
                msgs,
                "p1",
                session_id="s1",
                from_sequence=0,
                to_sequence=4,
            )
        )
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

        provider = StubModelProvider(
            scenarios=[
                StubScenario(
                    response_text='{"candidates": [{"kind": "preference", '
                    '"subject": "user", "predicate": "style", '
                    '"value": "concise", '
                    '"explicitness": "model_inference", '
                    '"confidence": 0.6, "importance": 0.4, '
                    '"evidence_message_ids": ["msg_1"]}]}',
                ),
            ]
        )
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        msgs = _msgs(["I write concise code"] * 5)
        items = asyncio.run(
            extractor.extract_from_messages(
                msgs,
                "p1",
                session_id="s1",
                from_sequence=0,
                to_sequence=4,
            )
        )
        assert len(items) == 1

        # 推断 → candidate
        row = db.execute("SELECT status, value FROM memory_items WHERE value='concise'").fetchone()
        assert row is not None
        assert row["status"] == "candidate"

    def test_parse_failure_raises_typed_error(self, db, service):
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(
            scenarios=[
                StubScenario(response_text="I don't see anything to extract here."),
            ]
        )
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router, strict=True)

        from cogito.service.memory_extractor import MemoryExtractionParseError

        with pytest.raises(MemoryExtractionParseError):
            asyncio.run(
                extractor.extract_from_messages(
                    _msgs(["Hello"] * 5),
                    "p1",
                    session_id="s1",
                )
            )

    def test_format_messages(self, db, service):
        msgs = _msgs(["Hello", "Hi there"])
        formatted = MemoryExtractor._format_messages(msgs)
        assert "[msg_0]: user: Hello" in formatted
        assert "[msg_1]: assistant: Hi there" in formatted


# ── PLAN-13 P13-02: 精确来源追溯 ──


class TestMemoryExtractorEvidence:
    def test_evidence_traced_to_memory_sources(self, db, service):
        """一条候选关联精确 Message 来源（P13-02 MEM-P00-01）。"""
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(
            scenarios=[
                StubScenario(
                    response_text='{"candidates": [{"kind": "preference", '
                    '"subject": "user", "predicate": "lang", '
                    '"value": "Python", '
                    '"explicitness": "explicit_user_statement", '
                    '"confidence": 0.95, "importance": 0.8, '
                    '"evidence_message_ids": ["msg_0", "msg_2"]}]}',
                ),
            ]
        )
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        msgs = _msgs(["I like Python"] * 5)
        items = asyncio.run(
            extractor.extract_from_messages(
                msgs,
                "p1",
                session_id="s1",
                from_sequence=0,
                to_sequence=4,
            )
        )
        assert len(items) == 1

        # 验证 memory_sources 有精确来源
        rows = db.execute("SELECT memory_id FROM memory_items WHERE value='Python'").fetchall()
        assert len(rows) == 1
        mid = rows[0]["memory_id"]
        src_rows = db.execute(
            "SELECT source_id, source_type FROM memory_sources "
            "WHERE memory_id=? AND deleted_at IS NULL",
            (mid,),
        ).fetchall()
        # 应有 2 条精确 message 来源
        assert len(src_rows) == 2
        src_ids = {r["source_id"] for r in src_rows}
        assert "msg_0" in src_ids
        assert "msg_2" in src_ids
        # source_type 遵循计划 §5.1 枚举（message，非 auto_extract）
        assert all(r["source_type"] == "message" for r in src_rows)

    def test_out_of_window_evidence_rejected(self, db, service):
        """模型返回窗口外 ID 被过滤（PLAN-13 P13-02 防伪造）。"""
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(
            scenarios=[
                StubScenario(
                    response_text='{"candidates": [{"kind": "preference", '
                    '"subject": "user", "predicate": "lang", '
                    '"value": "Python", '
                    '"explicitness": "explicit_user_statement", '
                    '"confidence": 0.95, "importance": 0.8, '
                    '"evidence_message_ids": ["msg_0", "fake_99"]}]}',
                ),
            ]
        )
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        msgs = _msgs(["I like Python"] * 5)
        items = asyncio.run(
            extractor.extract_from_messages(
                msgs,
                "p1",
                session_id="s1",
                from_sequence=0,
                to_sequence=4,
            )
        )
        assert len(items) == 1

        # 验证只有 msg_0 进入 memory_sources，fake_99 被过滤
        rows = db.execute("SELECT memory_id FROM memory_items WHERE value='Python'").fetchall()
        mid = rows[0]["memory_id"]
        src_rows = db.execute(
            "SELECT source_id FROM memory_sources WHERE memory_id=? AND deleted_at IS NULL", (mid,)
        ).fetchall()
        src_ids = {r["source_id"] for r in src_rows}
        assert "msg_0" in src_ids
        assert "fake_99" not in src_ids

    def test_no_auto_extract_source_id(self, db, service):
        """source_id 不再写死 auto_extract（P13-02 MEM-P00-01）。"""
        from cogito.model.router import ModelRouter
        from cogito.model.stub_provider import StubModelProvider, StubScenario

        provider = StubModelProvider(
            scenarios=[
                StubScenario(
                    response_text='{"candidates": [{"kind": "preference", '
                    '"subject": "user", "predicate": "lang", '
                    '"value": "Python", '
                    '"explicitness": "explicit_user_statement", '
                    '"confidence": 0.95, "importance": 0.8}]}',
                ),
            ]
        )
        router = ModelRouter(
            providers={"extractor": provider},
            role_map={"memory_extractor": "extractor"},
        )
        extractor = MemoryExtractor(db, service, router=router)

        msgs = _msgs(["I like Python"] * 5)
        asyncio.run(
            extractor.extract_from_messages(
                msgs,
                "p1",
                session_id="s1",
                from_sequence=0,
                to_sequence=4,
            )
        )

        # memory_items.source_id 应为 extraction_id，不是 auto_extract
        row = db.execute("SELECT source_id FROM memory_items WHERE value='Python'").fetchone()
        assert row is not None
        assert row["source_id"] != "auto_extract"
        assert "s1" in row["source_id"]  # 含 session_id 的 extraction_id
