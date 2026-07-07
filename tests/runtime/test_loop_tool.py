"""Tests for Agent Loop tool calling iteration.

覆盖场景：
- 单工具调用 → 继续 → final
- 多工具调用（顺序）
- 循环检测
- max_tool_calls 限制
- 无效参数 → 错误返回
- 没有 Executor 时的行为
"""

from __future__ import annotations

import pytest

from cogito.capability import CapabilityRegistry
from cogito.capability.executor import ToolExecutor
from cogito.capability.models import ToolContext, ToolDef
from cogito.model.contracts import (
    FinishReason,
    Usage,
)
from cogito.model.router import ModelRouter
from cogito.model.stub_provider import StubModelProvider, StubScenario
from cogito.runtime.context import ContextItem, ContextSnapshot
from cogito.runtime.loop import AgentLoop, LoopResultType


def _make_snapshot(text: str = "Hello") -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="snap1",
        turn_id="t1",
        session_id="s1",
        items=(
            ContextItem(
                item_type="message",
                item_id="m1",
                source="s1",
                tokens=5,
                content=text,
            ),
        ),
        total_tokens=5,
        created_at=1000,
    )


async def _echo_handler(args: dict, ctx: ToolContext) -> str:
    return args.get("text", "")


async def _noop_handler(args: dict, ctx: ToolContext) -> str:
    return "done"


def _make_registry() -> CapabilityRegistry:
    r = CapabilityRegistry()
    r.register(ToolDef(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
        handler=_echo_handler,
        risk_level="low",
    ))
    r.register(ToolDef(
        name="noop",
        description="Do nothing",
        input_schema={"type": "object", "properties": {}},
        handler=_noop_handler,
        risk_level="low",
    ))
    return r


def _make_loop(
    scenarios: list[StubScenario],
    toolsets: set[str] | None = None,
    registry: CapabilityRegistry | None = None,
    **kwargs,
) -> AgentLoop:
    provider = StubModelProvider(scenarios)
    router = ModelRouter(
        providers={"stub": provider},
        role_map={"main": "stub"},
    )
    resolved_registry = registry or _make_registry()
    executor = ToolExecutor(resolved_registry)

    return AgentLoop(
        router=router,
        registry=resolved_registry,
        executor=executor,
        toolsets=toolsets or {"core"},
        **kwargs,
    )


class TestToolCallIteration:
    @pytest.mark.asyncio
    async def test_single_tool_then_final(self):
        """模型先返回 tool_call → 执行 → 下一轮返回 final。"""
        loop = _make_loop([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "hi"}'}},
                ),
            ),
            StubScenario(
                response_text="Done!",
                finish_reason=FinishReason.stop,
            ),
        ])

        result = await loop.run(_make_snapshot())
        assert result.result_type == LoopResultType.final_response
        assert result.text == "Done!"
        assert result.tool_call_count == 1

    @pytest.mark.asyncio
    async def test_two_tools_then_final(self):
        """两个顺序工具调用后 final。"""
        loop = _make_loop([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "a"}'}},
                ),
            ),
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c2", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "b"}'}},
                ),
            ),
            StubScenario(
                response_text="All done",
                finish_reason=FinishReason.stop,
            ),
        ])

        result = await loop.run(_make_snapshot())
        assert result.result_type == LoopResultType.final_response
        assert result.text == "All done"
        assert result.tool_call_count == 2

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_in_single_response(self):
        """单次响应中多个并行 tool_calls。"""
        loop = _make_loop([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "a"}'}},
                    {"id": "c2", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "b"}'}},
                ),
            ),
            StubScenario(
                response_text="All executed",
                finish_reason=FinishReason.stop,
            ),
        ])

        result = await loop.run(_make_snapshot())
        assert result.result_type == LoopResultType.final_response
        assert result.tool_call_count == 2

    @pytest.mark.asyncio
    async def test_max_tool_calls_termination(self):
        """超过 max_tool_calls 限制时终止。"""
        # 无限返回 tool_calls（使用不同 id 避免重复跳过）
        loop = _make_loop(
            [
                StubScenario(
                    finish_reason=FinishReason.tool_calls,
                    tool_calls=(
                        {"id": f"c{i}", "type": "function",
                         "function": {"name": "echo", "arguments": '{"text": "' + str(i) + '"}'}},
                    ),
                )
                for i in range(10)
            ],
            max_tool_calls=3,
            max_repeated_tool_signature=10,
            max_iterations=100,
        )

        result = await loop.run(_make_snapshot())
        assert result.result_type == LoopResultType.max_tool_calls
        assert result.tool_call_count >= 3

    @pytest.mark.asyncio
    async def test_loop_detection_repeated_tool(self):
        """相同工具+参数连续重复达到阈值时终止。"""
        # 使用递增 ids 避免重复跳过，但相同工具名+参数触发检测
        loop = _make_loop(
            [
                StubScenario(
                    finish_reason=FinishReason.tool_calls,
                    tool_calls=(
                        {"id": f"c{i}", "type": "function",
                         "function": {"name": "echo", "arguments": '{"text": "repeat"}'}},
                    ),
                )
                for i in range(5)
            ],
            max_repeated_tool_signature=3,
        )

        result = await loop.run(_make_snapshot())
        assert result.result_type == LoopResultType.repetition

    @pytest.mark.asyncio
    async def test_tool_result_injected_in_next_request(self):
        """工具执行结果应出现在下一轮模型请求的消息中。"""
        provider = StubModelProvider([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "hello"}'}},
                ),
            ),
            StubScenario(
                response_text="OK",
                finish_reason=FinishReason.stop,
            ),
        ])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        registry = _make_registry()
        executor = ToolExecutor(registry)
        loop = AgentLoop(
            router=router,
            registry=registry,
            executor=executor,
            toolsets={"core"},
        )

        await loop.run(_make_snapshot())

        # 第二次请求应包含 tool role 消息
        assert len(provider.received_requests) >= 2
        second_req = provider.received_requests[1]

        tool_messages = [
            m for m in second_req.messages
            if m.get("role") == "tool"
        ]
        assert len(tool_messages) >= 1
        assert tool_messages[0]["tool_call_id"] == "c1"
        assert "hello" in tool_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_tool_mixed_with_text_content(self):
        """模型同时返回文本和 tool_calls。"""
        loop = _make_loop([
            StubScenario(
                response_text="I will use echo tool:",
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "test"}'}},
                ),
            ),
            StubScenario(
                response_text="Tool executed!",
                finish_reason=FinishReason.stop,
            ),
        ])

        result = await loop.run(_make_snapshot())
        assert result.result_type == LoopResultType.final_response
        assert result.tool_call_count == 1

    @pytest.mark.asyncio
    async def test_tool_usage_tracked_in_loop_result(self):
        """LoopResult 累计 tool 调用次数。"""
        loop = _make_loop([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "a"}'}},
                ),
            ),
            StubScenario(
                response_text="Final",
                finish_reason=FinishReason.stop,
            ),
        ])

        result = await loop.run(_make_snapshot())
        assert result.tool_call_count == 1


class TestToolRepair:
    """Agent-LOOP / 5: 无效参数修复。"""

    @pytest.mark.asyncio
    async def test_validation_error_triggers_repair(self):
        """参数校验失败后发送修复提示，模型修正后成功。"""
        provider = StubModelProvider([
            # 第一次：缺少必填参数 text
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}},
                ),
            ),
            # 第二次：修正后正确调用
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c2", "type": "function",
                     "function": {"name": "echo", "arguments": '{"text": "fixed"}'}},
                ),
            ),
            # 第三次：最终回复
            StubScenario(
                response_text="Done after fix",
                finish_reason=FinishReason.stop,
            ),
        ])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        registry = _make_registry()
        executor = ToolExecutor(registry)
        loop = AgentLoop(
            router=router,
            registry=registry,
            executor=executor,
            toolsets={"core"},
        )

        result = await loop.run(_make_snapshot())
        assert result.result_type == LoopResultType.final_response
        assert result.text == "Done after fix"
        assert result.tool_call_count == 1  # 修复不计入

    @pytest.mark.asyncio
    async def test_repeated_validation_error_counts(self):
        """修复后仍然参数错误，第二次计入 tool call。"""
        provider = StubModelProvider([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}},
                ),
            ),
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c2", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}},
                ),
            ),
            StubScenario(
                response_text="Final",
                finish_reason=FinishReason.stop,
            ),
        ])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        registry = _make_registry()
        executor = ToolExecutor(registry)
        loop = AgentLoop(
            router=router,
            registry=registry,
            executor=executor,
            toolsets={"core"},
        )

        result = await loop.run(_make_snapshot())
        assert result.result_type == LoopResultType.final_response
        # 第一次修复不计入，第二次计入
        assert result.tool_call_count == 1
