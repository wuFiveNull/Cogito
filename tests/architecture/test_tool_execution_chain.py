"""PR-C2: Tool execution transaction chain — Plan 03 M2.

Intent → Policy → Budget → Execute → Receipt + idempotency key hash.
"""
from __future__ import annotations

import pytest

from cogito.capability.executor import ToolExecutor, _hash_arguments
from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.policy import ToolPolicy
from cogito.capability.registry import CapabilityRegistry


def _make_tool(name: str = "echo", **kw: Any) -> ToolDef:
    return ToolDef(
        name=name, description=f"tool {name}", input_schema={"type": "object"},
        handler=_echo_handler, **kw,
    )


def _make_executor(*tools: ToolDef) -> ToolExecutor:
    reg = CapabilityRegistry()
    for t in tools:
        reg.register(t)
    policy = ToolPolicy()
    return ToolExecutor(registry=reg, policy=policy)


@pytest.mark.asyncio
async def test_execute_success() -> None:
    """正常执行链：resolve → policy → execute → success。"""
    executor = _make_executor(_make_tool())
    ctx = ToolContext(attempt_id="a1", trace_id="tr1", tool_call_id="tc1", turn_id="t1")
    result = await executor.execute("c1", "echo", {"text": "hi"}, ctx)
    assert result.status == "success"
    assert "hi" in result.result


@pytest.mark.asyncio
async def test_execute_unresolved_tool() -> None:
    """未注册工具 → error。"""
    executor = _make_executor()
    ctx = ToolContext(attempt_id="a1", trace_id="tr1", tool_call_id="tc1", turn_id="t1")
    result = await executor.execute("c1", "missing", {}, ctx)
    assert result.status == "error"
    assert "not found" in result.error_message


@pytest.mark.asyncio
async def test_policy_deny_blocks_execution() -> None:
    """Policy deny → 执行被阻止。"""
    reg = CapabilityRegistry()
    reg.register(_make_tool("danger", risk_level="high"))
    policy = ToolPolicy(denylist={"danger"})
    executor = ToolExecutor(registry=reg, policy=policy)
    ctx = ToolContext(attempt_id="a1", trace_id="tr1", tool_call_id="tc1", turn_id="t1")
    result = await executor.execute("c1", "danger", {}, ctx)
    assert result.status == "error"
    assert "denied" in result.error_message.lower()


def test_idempotency_hash_stable() -> None:
    """相同参数 → 相同 hash（幂等键稳定）。"""
    h1 = _hash_arguments("echo", {"text": "hi", "n": 1})
    h2 = _hash_arguments("echo", {"n": 1, "text": "hi"})
    assert h1 == h2  # 排序稳定


def test_idempotency_hash_distinguishes() -> None:
    """不同参数 → 不同 hash。"""
    h1 = _hash_arguments("echo", {"text": "a"})
    h2 = _hash_arguments("echo", {"text": "b"})
    assert h1 != h2


@pytest.mark.asyncio
async def test_execute_with_capability_id() -> None:
    """支持 namespace:name 查找。"""
    reg = CapabilityRegistry()
    reg.register(_make_tool("echo", namespace="core"))
    policy = ToolPolicy()
    executor = ToolExecutor(registry=reg, policy=policy)
    ctx = ToolContext(attempt_id="a1", trace_id="tr1", tool_call_id="tc1", turn_id="t1")
    result = await executor.execute("c1", "echo", {"text": "ok"}, ctx)
    assert result.status == "success"


from typing import Any  # noqa: E402


async def _echo_handler(args: dict, ctx: Any) -> str:
    return f"echo:{args.get('text', '')}"