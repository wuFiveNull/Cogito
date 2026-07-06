"""Agent Loop — 模型执行循环。

AGENT-LOOP / 2. LoopState：循环内状态和可恢复字段。
AGENT-LOOP / 3. 单轮协议：统一 ModelResponse 输出类型。
AGENT-LOOP / 5. 输出校验与修复：无效输出最多修复一次。
AGENT-LOOP / 6. 终止条件。

当前阶段只处理 FinalResponse、Refusal、InvalidOutput 和 Provider terminal error。
Provider 返回 Tool Call 时返回明确的"不支持 Tool"受控失败。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from cogito.model.contracts import (
    ContentPart,
    ErrorCategory,
    ErrorEnvelope,
    FinishReason,
    ModelRequest,
    ModelResponse,
    Usage,
)
from cogito.model.provider import ModelProvider
from cogito.model.router import ModelRouter, RouterError
from cogito.runtime.context import ContextSnapshot


class LoopResultType(StrEnum):
    """Agent Loop 的标准输出类型。"""
    final_response = "final_response"
    refusal = "refusal"
    invalid_output = "invalid_output"
    error = "error"
    cancelled = "cancelled"
    max_iterations = "max_iterations"
    max_tokens = "max_tokens"
    max_runtime = "max_runtime"
    repetition = "repetition"
    unsupported_tool = "unsupported_tool"


@dataclass(frozen=True)
class LoopResult:
    """Agent Loop 的最终输出。"""
    result_type: LoopResultType = LoopResultType.final_response
    content_parts: tuple[ContentPart, ...] = ()
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    iterations: int = 0
    error_message: str = ""
    finish_reason: FinishReason = FinishReason.stop

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_parts", tuple(self.content_parts))

    @property
    def is_success(self) -> bool:
        return self.result_type == LoopResultType.final_response


@dataclass
class LoopState:
    """Agent Loop 运行时状态。"""
    turn_id: str = ""
    attempt_id: str = ""
    iteration_no: int = 0
    context_snapshot_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    completed_tool_call_ids: set[str] = field(default_factory=set)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    partial_output_ref: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: FinishReason = FinishReason.stop
    started_at: datetime | None = None
    last_iteration_at: datetime | None = None
    total_latency_ms: int = 0


class AgentLoop:
    """模型执行循环。

    MVP 只处理：
    1. 构建 ModelRequest（从 ContextSnapshot）
    2. 调用 Provider
    3. 检查响应（FinalResponse/Refusal/InvalidOutput/ToolCall）
    4. 终止条件检查
    5. 输出修复（InvalidOutput → 最多修复一次）
    """

    def __init__(
        self,
        router: ModelRouter,
        max_iterations: int = 10,
        max_runtime_s: int = 120,
        max_total_tokens: int = 32000,
    ) -> None:
        self._router = router
        self._max_iterations = max_iterations
        self._max_runtime = timedelta(seconds=max_runtime_s)
        self._max_total_tokens = max_total_tokens

    async def run(
        self,
        context: ContextSnapshot,
        model_role: str = "main",
        cancel_flag: Callable[[], bool] | None = None,
    ) -> LoopResult:
        """执行 Agent Loop。"""
        state = LoopState(
            turn_id=context.turn_id,
            context_snapshot_id=context.snapshot_id,
            started_at=datetime.now(timezone.utc),
            iteration_no=0,
        )

        output_repaired = False

        while True:
            state.iteration_no += 1
            state.last_iteration_at = datetime.now(timezone.utc)

            # ── 检查取消 ──
            if cancel_flag and cancel_flag():
                return self._make_result(LoopResultType.cancelled, state,
                                         "Cancelled by request")

            # ── 终止条件检查（迭代次数）──
            if state.iteration_no > self._max_iterations:
                return self._make_result(LoopResultType.max_iterations, state,
                                         f"Exceeded max iterations ({self._max_iterations})")

            # ── 终止条件检查（运行时间）──
            elapsed = state.last_iteration_at - state.started_at
            if elapsed > self._max_runtime:
                return self._make_result(LoopResultType.max_runtime, state,
                                         f"Exceeded max runtime ({self._max_runtime})")

            # ── 终止条件检查（总 Token）──
            if state.usage.total_tokens > self._max_total_tokens:
                return self._make_result(LoopResultType.max_tokens, state,
                                         f"Exceeded max tokens ({self._max_total_tokens})")

            # ── 构建 ModelRequest ──
            request = self._build_request(state, context)

            # ── 调用 Provider ──
            try:
                iter_start = datetime.now(timezone.utc)
                response = await self._router.generate(
                    request, model_role=model_role,
                )
                iter_latency = int(
                    (datetime.now(timezone.utc) - iter_start).total_seconds() * 1000
                )
            except RouterError as e:
                return self._make_result(
                    LoopResultType.error, state,
                    f"Provider error: {e}",
                    finish_reason=FinishReason.error,
                )

            # 累计 Usage
            state.usage = state.usage + response.usage
            state.total_latency_ms += iter_latency

            # ── 检查响应 ──
            result = self._classify_response(response)

            if result == "_final":
                return self._make_result(
                    LoopResultType.final_response, state,
                    response.text,
                    content_parts=list(response.content_parts),
                    usage=state.usage,
                    finish_reason=response.finish_reason,
                )
            elif result == "_refusal":
                return self._make_result(
                    LoopResultType.refusal, state,
                    response.text,
                    content_parts=list(response.content_parts),
                )
            elif result == "_tool_call":
                # 当前阶段不支持 Tool，返回受控失败
                return self._make_result(
                    LoopResultType.unsupported_tool, state,
                    "Tool calls are not supported in this version",
                )
            elif result == "_invalid":
                if output_repaired:
                    return self._make_result(
                        LoopResultType.invalid_output, state,
                        "Invalid output after repair attempt",
                    )
                # 修复一次
                output_repaired = True
                continue

            # fallback
            return self._make_result(LoopResultType.error, state,
                                     f"Unknown response type: {result}")

    def _build_request(
        self, state: LoopState, context: ContextSnapshot,
    ) -> ModelRequest:
        """从 ContextSnapshot 和 LoopState 构建 ModelRequest。"""
        messages = []
        for item in context.items:
            role = "system" if item.item_type == "system_policy" else "user"
            messages.append({
                "role": role,
                "content": item.content,
            })
        # 添加历史消息
        messages.extend(state.messages)

        return ModelRequest(
            messages=messages,
            stream=False,
        )

    def _classify_response(self, response: ModelResponse) -> str:
        """分类 ModelResponse 类型。"""
        if response.finish_reason == FinishReason.stop:
            if response.text.strip() == "":
                return "_invalid"
            return "_final"

        if response.finish_reason == FinishReason.tool_calls:
            return "_tool_call"

        if response.finish_reason == FinishReason.error:
            return "_error"

        if response.finish_reason == FinishReason.content_filter:
            return "_refusal"

        if response.finish_reason == FinishReason.length:
            if response.text.strip():
                return "_final"
            return "_invalid"

        if response.finish_reason == FinishReason.cancelled:
            return "_error"

        return "_invalid"

    def _make_result(
        self,
        result_type: LoopResultType,
        state: LoopState,
        text: str,
        content_parts: list[ContentPart] | None = None,
        usage: Usage | None = None,
        finish_reason: FinishReason = FinishReason.stop,
    ) -> LoopResult:
        return LoopResult(
            result_type=result_type,
            content_parts=tuple(content_parts or [ContentPart(text=text)]),
            text=text,
            usage=usage or state.usage,
            latency_ms=state.total_latency_ms,
            iterations=state.iteration_no,
            finish_reason=finish_reason,
        )


# 运行时类型修复
from collections.abc import Callable  # noqa: E402
