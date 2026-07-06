"""Tests for StubModelProvider tool call simulation.

覆盖场景：
- 工具调用响应
- 预设工具调用序列
- 并行工具调用
- 混合文本+工具
"""

from __future__ import annotations

import pytest

from cogito.model.contracts import FinishReason, ModelRequest
from cogito.model.stub_provider import StubModelProvider, StubScenario


class TestStubToolCalls:
    @pytest.mark.asyncio
    async def test_single_tool_call(self):
        provider = StubModelProvider([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": '{"text": "hello"}',
                        },
                    },
                ),
            ),
        ])

        response = await provider.generate(ModelRequest())
        assert response.finish_reason == FinishReason.tool_calls
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0]["function"]["name"] == "echo"
        assert response.tool_calls[0]["id"] == "call_abc"

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self):
        provider = StubModelProvider([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                    {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
                ),
            ),
        ])

        response = await provider.generate(ModelRequest())
        assert response.finish_reason == FinishReason.tool_calls
        assert len(response.tool_calls) == 2

    @pytest.mark.asyncio
    async def test_mixed_text_and_tool(self):
        """模型同时返回文本和工具调用。"""
        provider = StubModelProvider([
            StubScenario(
                response_text="I will use a tool.",
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"text":"ok"}'}},
                ),
            ),
        ])

        response = await provider.generate(ModelRequest())
        assert response.finish_reason == FinishReason.tool_calls
        assert "tool" in response.text.lower()
        assert len(response.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_tool_call_sequence(self):
        """多轮调用返回不同的工具。"""
        provider = StubModelProvider([
            StubScenario(
                finish_reason=FinishReason.tool_calls,
                tool_calls=(
                    {"id": "c1", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
                ),
            ),
            StubScenario(
                response_text="Final answer",
                finish_reason=FinishReason.stop,
            ),
        ])

        r1 = await provider.generate(ModelRequest())
        assert r1.finish_reason == FinishReason.tool_calls
        assert r1.tool_calls[0]["function"]["name"] == "tool_a"

        r2 = await provider.generate(ModelRequest())
        assert r2.finish_reason == FinishReason.stop
        assert r2.text == "Final answer"
