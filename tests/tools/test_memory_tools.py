"""Tests for remember / recall / forget memory tools.

PLAN-09 M4a: 工具签名改为 MemoryReader / MemoryWriter 端口而非
SqliteMemoryService 具体实现；测试使用协议类型传入。

覆盖场景：
- remember_memory 无 writer 时返回提示
- remember_memory 缺少必填参数时返回提示
- remember_memory 幂等同值返回同一 memory_id
- forget_memory 无 writer / 无参数 / 各删除模式
- recall_memory 检索相关记忆
"""
from __future__ import annotations

import sqlite3

import pytest

from cogito.capability.models import ToolContext
from cogito.contracts.memory import MemoryReader, MemoryWriter
from cogito.service.memory_service import SqliteMemoryService
from cogito.store.migration import migrate


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(
        attempt_id="a1",
        trace_id="t1",
        tool_call_id="tc1",
        principal_id="p1",
        session_id="s1",
    )


@pytest.fixture
def ctx_no_principal() -> ToolContext:
    return ToolContext(
        attempt_id="a1",
        trace_id="t1",
        tool_call_id="tc1",
        principal_id="",
        session_id="s1",
    )


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


@pytest.fixture
def memory_writer(service) -> MemoryWriter:
    return service


@pytest.fixture
def memory_reader(service) -> MemoryReader:
    return service


# ──────────────────────────────────────────────
# remember_memory
# ──────────────────────────────────────────────


class TestRememberMemoryTool:
    @pytest.mark.asyncio
    async def test_remember_no_writer(self, ctx):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(writer=None)
        result = await tool_def.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )
        assert "memory writer not available" in result.lower()

    @pytest.mark.asyncio
    async def test_remember_no_principal(self, ctx_no_principal):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(writer=None)
        result = await tool_def.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"},
            ctx_no_principal,
        )
        assert "principal not available" in result.lower()

    @pytest.mark.asyncio
    async def test_remember_empty_input(self, ctx):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(writer=None)
        result = await tool_def.handler({"subject": "", "predicate": "", "value": ""}, ctx)
        assert "at least one" in result.lower()

    @pytest.mark.asyncio
    async def test_remember_saves_memory(self, ctx, memory_writer):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(writer=memory_writer)
        result = await tool_def.handler(
            {
                "subject": "user",
                "predicate": "preferred_language",
                "value": "Python",
                "kind": "preference",
            },
            ctx,
        )
        assert "Saved memory" in result
        assert "preference" in result
        assert "Python" in result

    @pytest.mark.asyncio
    async def test_remember_idempotent(self, ctx, memory_writer):
        """同值重复调用返回同一 memory_id。"""
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(writer=memory_writer)
        r1 = await tool_def.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )
        r2 = await tool_def.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )
        # 提取 memory_id
        id1 = r1.split("memory_id=")[-1].strip(")")
        id2 = r2.split("memory_id=")[-1].strip(")")
        assert id1 == id2

    @pytest.mark.asyncio
    async def test_remember_schema(self, ctx):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def()
        assert tool_def.name == "remember_memory"
        assert tool_def.risk_level == "low"
        assert tool_def.side_effect_class == "idempotent"
        assert tool_def.permissions == ("memory.write",)
        props = tool_def.input_schema["properties"]
        assert "subject" in props
        assert "predicate" in props
        assert "value" in props
        assert "kind" in props
        assert "scope_type" in props
        assert "scope_id" in props


# ──────────────────────────────────────────────
# forget_memory
# ──────────────────────────────────────────────


class TestForgetMemoryTool:
    @pytest.mark.asyncio
    async def test_forget_no_writer(self, ctx):
        from cogito.tools.forget_memory import create_tool_def

        tool_def = create_tool_def(reader=None, writer=None)
        result = await tool_def.handler({"memory_id": "abc"}, ctx)
        assert "memory writer not available" in result.lower()

    @pytest.mark.asyncio
    async def test_forget_no_principal(self, ctx_no_principal):
        from cogito.tools.forget_memory import create_tool_def

        tool_def = create_tool_def(reader=None, writer=None)
        result = await tool_def.handler({"memory_id": "abc"}, ctx_no_principal)
        assert "principal not available" in result.lower()

    @pytest.mark.asyncio
    async def test_forget_no_params(self, ctx, memory_writer):
        """提供 writer 但无参数时，返回用法提示。"""
        from cogito.tools.forget_memory import create_tool_def

        tool_def = create_tool_def(reader=None, writer=memory_writer)
        result = await tool_def.handler({}, ctx)
        assert "specify one of" in result.lower()
        assert "memory_id" in result

    @pytest.mark.asyncio
    async def test_forget_by_id(self, ctx, memory_writer, memory_reader):
        from cogito.tools.remember_memory import create_tool_def as create_remember

        # 先创建一条记忆
        remember = create_remember(writer=memory_writer)
        saved = await remember.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )
        memory_id = saved.split("memory_id=")[-1].strip(")")

        from cogito.tools.forget_memory import create_tool_def as create_forget

        forget = create_forget(reader=memory_reader, writer=memory_writer)
        result = await forget.handler({"memory_id": memory_id}, ctx)
        assert "Forgot memory" in result
        assert memory_id in result

        # 验证已删除
        from cogito.tools.recall_memory import create_tool_def as create_recall

        recall = create_recall(reader=memory_reader)
        found = await recall.handler({"query": "Python"}, ctx)
        assert "No memories found" in found

    @pytest.mark.asyncio
    async def test_forget_by_subject_predicate(self, ctx, memory_writer):
        from cogito.tools.remember_memory import create_tool_def as create_remember

        remember = create_remember(writer=memory_writer)
        await remember.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )

        from cogito.tools.forget_memory import create_tool_def as create_forget

        forget = create_forget(reader=None, writer=memory_writer)
        result = await forget.handler({"subject": "user", "predicate": "lang"}, ctx)
        assert "Forgot memory" in result

    @pytest.mark.asyncio
    async def test_forget_by_query_finds_candidates(self, ctx, memory_writer, memory_reader):
        from cogito.tools.remember_memory import create_tool_def as create_remember

        remember = create_remember(writer=memory_writer)
        await remember.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )
        await remember.handler(
            {"subject": "user", "predicate": "editor", "value": "VS Code"}, ctx,
        )

        from cogito.tools.forget_memory import create_tool_def as create_forget

        forget = create_forget(reader=memory_reader, writer=memory_writer)
        result = await forget.handler({"query": "editor"}, ctx)
        assert "Found" in result
        assert "VS Code" in result
        assert "memory_id=" in result

    @pytest.mark.asyncio
    async def test_forget_nonexistent_id(self, ctx, memory_writer):
        from cogito.tools.forget_memory import create_tool_def as create_forget

        forget = create_forget(writer=memory_writer)
        result = await forget.handler({"memory_id": "nonexistent"}, ctx)
        assert "No memory found" in result

    @pytest.mark.asyncio
    async def test_forget_schema(self, ctx):
        from cogito.tools.forget_memory import create_tool_def

        tool_def = create_tool_def()
        assert tool_def.name == "forget_memory"
        assert tool_def.risk_level == "high"
        assert tool_def.approval_policy == "always"
        assert tool_def.side_effect_class == "idempotent"
        assert tool_def.permissions == ("memory.delete",)
        props = tool_def.input_schema["properties"]
        assert "memory_id" in props
        assert "subject" in props
        assert "predicate" in props
        assert "query" in props
