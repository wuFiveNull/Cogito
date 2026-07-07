"""Tests for remember_memory and forget_memory tools.

覆盖场景：
- remember_memory 无 service 时返回提示
- remember_memory 缺少必填参数时返回提示
- remember_memory Schema 定义完整
- forget_memory 无 service 时返回提示
- forget_memory 缺少参数时返回提示
- forget_memory 各模式（memory_id, subject+predicate, query）
- forget_memory Schema 定义完整
"""

from __future__ import annotations

import sqlite3

import pytest

from cogito.capability.models import ToolContext
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


# ──────────────────────────────────────────────
# remember_memory
# ──────────────────────────────────────────────


class TestRememberMemoryTool:
    @pytest.mark.asyncio
    async def test_remember_no_service(self, ctx):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(service=None)
        result = await tool_def.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )
        assert "memory service not available" in result.lower()

    @pytest.mark.asyncio
    async def test_remember_no_principal(self, ctx_no_principal):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(service=None)
        result = await tool_def.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"},
            ctx_no_principal,
        )
        assert "principal not available" in result.lower()

    @pytest.mark.asyncio
    async def test_remember_empty_input(self, ctx):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(service=None)
        result = await tool_def.handler({"subject": "", "predicate": "", "value": ""}, ctx)
        assert "at least one" in result.lower()

    @pytest.mark.asyncio
    async def test_remember_saves_memory(self, ctx, service):
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(service=service)
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
    async def test_remember_idempotent(self, ctx, service):
        """同值重复调用返回同一 memory_id。"""
        from cogito.tools.remember_memory import create_tool_def

        tool_def = create_tool_def(service=service)
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
    async def test_forget_no_service(self, ctx):
        from cogito.tools.forget_memory import create_tool_def

        tool_def = create_tool_def(service=None)
        result = await tool_def.handler({"memory_id": "abc"}, ctx)
        assert "memory service not available" in result.lower()

    @pytest.mark.asyncio
    async def test_forget_no_principal(self, ctx_no_principal):
        from cogito.tools.forget_memory import create_tool_def

        tool_def = create_tool_def(service=None)
        result = await tool_def.handler({"memory_id": "abc"}, ctx_no_principal)
        assert "principal not available" in result.lower()

    @pytest.mark.asyncio
    async def test_forget_no_params(self, ctx, service):
        """提供 service 但无参数时，返回用法提示。"""
        from cogito.tools.forget_memory import create_tool_def

        tool_def = create_tool_def(service=service)
        result = await tool_def.handler({}, ctx)
        assert "specify one of" in result.lower()
        assert "memory_id" in result

    @pytest.mark.asyncio
    async def test_forget_by_id(self, ctx, service):
        # 先创建一条记忆
        from cogito.tools.remember_memory import create_tool_def as create_remember

        # 先创建一条记忆
        remember = create_remember(service=service)
        saved = await remember.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )
        memory_id = saved.split("memory_id=")[-1].strip(")")

        # 用 ID 删除
        from cogito.tools.forget_memory import create_tool_def as create_forget

        forget = create_forget(service=service)
        result = await forget.handler({"memory_id": memory_id}, ctx)
        assert "Forgot memory" in result
        assert memory_id in result

        # 验证已删除
        from cogito.tools.recall_memory import create_tool_def as create_recall

        recall = create_recall(service=service)
        found = await recall.handler({"query": "Python"}, ctx)
        assert "No memories found" in found

    @pytest.mark.asyncio
    async def test_forget_by_subject_predicate(self, ctx, service):
        from cogito.tools.forget_memory import create_tool_def as create_forget
        from cogito.tools.remember_memory import create_tool_def as create_remember

        remember = create_remember(service=service)
        await remember.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )

        forget = create_forget(service=service)
        result = await forget.handler({"subject": "user", "predicate": "lang"}, ctx)
        assert "Forgot memory" in result

    @pytest.mark.asyncio
    async def test_forget_by_query_finds_candidates(self, ctx, service):
        from cogito.tools.forget_memory import create_tool_def as create_forget
        from cogito.tools.remember_memory import create_tool_def as create_remember

        remember = create_remember(service=service)
        await remember.handler(
            {"subject": "user", "predicate": "lang", "value": "Python"}, ctx,
        )
        await remember.handler(
            {"subject": "user", "predicate": "editor", "value": "VS Code"}, ctx,
        )

        forget = create_forget(service=service)
        result = await forget.handler({"query": "editor"}, ctx)
        assert "Found" in result
        assert "VS Code" in result
        # 只返回候选，不删除
        assert "memory_id=" in result

    @pytest.mark.asyncio
    async def test_forget_nonexistent_id(self, ctx, service):
        from cogito.tools.forget_memory import create_tool_def as create_forget

        forget = create_forget(service=service)
        result = await forget.handler({"memory_id": "nonexistent"}, ctx)
        assert "No memory found" in result

    @pytest.mark.asyncio
    async def test_forget_schema(self, ctx):
        from cogito.tools.forget_memory import create_tool_def

        tool_def = create_tool_def()
        assert tool_def.name == "forget_memory"
        assert tool_def.risk_level == "low"
        props = tool_def.input_schema["properties"]
        assert "memory_id" in props
        assert "subject" in props
        assert "predicate" in props
        assert "query" in props
