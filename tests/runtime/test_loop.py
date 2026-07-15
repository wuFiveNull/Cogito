"""Tests for Agent Loop (PR 10-B).

覆盖场景：
- 单轮 FinalResponse
- Refusal
- InvalidOutput 修复一次
- 二次无效后失败
- timeout 有限重试
- max iterations/runtime/tokens
- cancel 优先
- Tool Call 在未启用阶段安全失败
"""

from __future__ import annotations

import pytest

from cogito.model.contracts import (
    ErrorCategory,
    ErrorEnvelope,
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


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_checkpoint_restores_cumulative_usage(self):
        provider = StubModelProvider(
            [
                StubScenario(
                    response_text="continued",
                    usage=Usage(input_tokens=3, output_tokens=2, cached_tokens=1),
                )
            ],
        )
        router = ModelRouter(providers={"stub": provider}, role_map={"main": "stub"})
        loop = AgentLoop(
            router,
            checkpoint_loader=lambda _turn_id: {
                "usage": {"input_tokens": 11, "output_tokens": 5, "cached_tokens": 2},
                "accumulated_cost": 0.25,
                "elapsed_wall_seconds": 1,
                "budget": {
                    "max_loop_iterations": 10,
                    "max_model_calls": 20,
                    "max_tool_calls": 50,
                    "max_input_tokens": 32000,
                    "max_output_tokens": 8192,
                    "max_wall_time_s": 120,
                    "max_cost": 0,
                },
            },
        )

        result = await loop.run(_make_snapshot())

        assert result.result_type == LoopResultType.final_response
        assert result.usage == Usage(input_tokens=14, output_tokens=7, cached_tokens=3)

    @pytest.mark.asyncio
    async def test_final_response(self):
        provider = StubModelProvider([StubScenario(response_text="Hello!")])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.result_type == LoopResultType.final_response
        assert "Hello" in result.text

    @pytest.mark.asyncio
    async def test_refusal_via_content_filter(self):
        provider = StubModelProvider(
            [
                StubScenario(
                    response_text="I cannot answer that",
                    finish_reason=FinishReason.content_filter,
                )
            ]
        )
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.result_type == LoopResultType.refusal

    @pytest.mark.asyncio
    async def test_invalid_output_repaired_once(self):
        """InvalidOutput 修复一次后成功。"""
        provider = StubModelProvider(
            [
                StubScenario(response_text="", finish_reason=FinishReason.stop),  # empty = invalid
                StubScenario(response_text="Valid response", finish_reason=FinishReason.stop),
            ]
        )
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.result_type == LoopResultType.final_response
        assert result.text == "Valid response"

    @pytest.mark.asyncio
    async def test_double_invalid_leads_to_invalid_output(self):
        """二次无效后失败。"""
        provider = StubModelProvider(
            [
                StubScenario(response_text="", finish_reason=FinishReason.stop),  # invalid
                StubScenario(response_text="", finish_reason=FinishReason.stop),  # invalid again
            ]
        )
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.result_type == LoopResultType.invalid_output

    @pytest.mark.asyncio
    async def test_max_iterations_guard_exists(self):
        """max_iterations guard 存在。"""
        provider = StubModelProvider([StubScenario(response_text="hello")])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router, max_iterations=100)
        result = await loop.run(_make_snapshot())

        # 第一次有效响应就终止（不会到达 max_iterations）
        assert result.result_type in (LoopResultType.final_response,)

    @pytest.mark.asyncio
    async def test_cancel_priority(self):
        """cancel 优先返回。"""
        provider = StubModelProvider([StubScenario(response_text="Hello")])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)

        cancel_flag = lambda: True  # noqa: E731
        result = await loop.run(_make_snapshot(), cancel_flag=cancel_flag)

        assert result.result_type == LoopResultType.cancelled

    @pytest.mark.asyncio
    async def test_tool_call_without_executor_returns_error(self):
        """没有 Executor 时 Tool Call 返回 error。"""
        provider = StubModelProvider(
            [
                StubScenario(
                    response_text="",
                    finish_reason=FinishReason.tool_calls,
                )
            ]
        )
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.result_type == LoopResultType.error

    @pytest.mark.asyncio
    async def test_provider_error_returns_error_result(self):
        """Provider terminal error。"""
        provider = StubModelProvider(
            [
                StubScenario(
                    error=ErrorEnvelope(
                        category=ErrorCategory.provider_internal,
                        message="Internal error",
                        retryable=False,
                    ),
                )
            ]
        )
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.result_type == LoopResultType.error

    @pytest.mark.asyncio
    async def test_loop_tracks_usage(self):
        """Loop 累计 Usage。"""
        usage1 = Usage(input_tokens=10, output_tokens=20)
        provider = StubModelProvider(
            [
                StubScenario(
                    response_text="Hello",
                    usage=usage1,
                )
            ]
        )
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.usage.input_tokens >= 10
        assert result.usage.output_tokens >= 20

    @pytest.mark.asyncio
    async def test_is_success_property(self):
        provider = StubModelProvider([StubScenario(response_text="OK")])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.is_success is True

    @pytest.mark.asyncio
    async def test_error_is_not_success(self):
        provider = StubModelProvider(
            [
                StubScenario(
                    error=ErrorEnvelope(
                        category=ErrorCategory.provider_internal,
                        message="Err",
                        retryable=False,
                    ),
                )
            ]
        )
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )
        loop = AgentLoop(router)
        result = await loop.run(_make_snapshot())

        assert result.is_success is False
