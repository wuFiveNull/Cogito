"""Agent Loop — 模型执行循环。

AGENT-LOOP / 2. LoopState：循环内状态和可恢复字段。
AGENT-LOOP / 3. 单轮协议：统一 ModelResponse 输出类型。
AGENT-LOOP / 4. Tool Call：执行并迭代。
AGENT-LOOP / 5. 输出校验与修复：无效输出最多修复一次。
AGENT-LOOP / 6. 终止条件。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

_LOGGER = logging.getLogger(__name__)

from cogito.capability.executor import ToolExecutor
from cogito.capability.models import ToolContext
from cogito.capability.registry import CapabilityRegistry
from cogito.model.contracts import (
    ContentPart,
    FinishReason,
    ModelRequest,
    ModelResponse,
    Usage,
)
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
    max_tool_calls = "max_tool_calls"
    max_tokens = "max_tokens"
    max_runtime = "max_runtime"
    repetition = "repetition"


@dataclass(frozen=True)
class LoopResult:
    """Agent Loop 的最终输出。"""
    result_type: LoopResultType = LoopResultType.final_response
    content_parts: tuple[ContentPart, ...] = ()
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    iterations: int = 0
    tool_call_count: int = 0
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
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    partial_output_ref: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: FinishReason = FinishReason.stop
    started_at: datetime | None = None
    last_iteration_at: datetime | None = None
    total_latency_ms: int = 0
    # Tool calling
    tool_call_count: int = 0
    tool_signatures: list[tuple[str, str]] = field(default_factory=list)
    tool_repaired: bool = False  # 本 attempt 是否已做过一次参数修复（AGENT-LOOP / 5）


class AgentLoop:
    """模型执行循环。

    流程：
    1. 构建 ModelRequest（含 tools）
    2. 调用 Provider
    3. 检查响应
       a. FinalResponse → 返回
       b. Refusal → 返回
       c. ToolCalls → 执行工具，结果加入 messages，继续
       d. Invalid → 修复一次 / 返回
       e. Error → 返回
    4. 终止条件（cancel, max_iterations, max_runtime, max_tokens, repetition）
    """

    def __init__(
        self,
        router: ModelRouter,
        registry: CapabilityRegistry | None = None,
        executor: ToolExecutor | None = None,
        toolsets: set[str] | None = None,
        max_iterations: int = 10,
        max_tool_calls: int = 50,
        max_repeated_tool_signature: int = 3,
        max_runtime_s: int = 120,
        max_total_tokens: int = 32000,
        checkpoint_callback: Callable[[dict], None] | None = None,
    ) -> None:
        self._router = router
        self._registry = registry
        self._executor = executor
        self._toolsets = toolsets or set()
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._max_repeated_tool_signature = max_repeated_tool_signature
        self._max_runtime = timedelta(seconds=max_runtime_s)
        self._max_total_tokens = max_total_tokens
        self._checkpoint_callback = checkpoint_callback

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
            started_at=datetime.now(UTC),
            iteration_no=0,
        )

        output_repaired = False

        while True:
            state.iteration_no += 1
            state.last_iteration_at = datetime.now(UTC)

            # ── 检查取消 ──
            if cancel_flag and cancel_flag():
                return self._make_result(LoopResultType.cancelled, state,
                                         "Cancelled by request")

            # ── 终止条件检查（迭代次数）──
            if state.iteration_no > self._max_iterations:
                return self._make_result(LoopResultType.max_iterations, state,
                                         f"Exceeded max iterations ({self._max_iterations})")

            # ── 终止条件检查（Tool 调用次数）──
            if state.tool_call_count >= self._max_tool_calls:
                return self._make_result(
                    LoopResultType.max_tool_calls, state,
                    f"Exceeded max tool calls ({self._max_tool_calls})",
                )

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
                iter_start = datetime.now(UTC)
                response = await self._router.generate(
                    request, model_role=model_role,
                )
                iter_latency = int(
                    (datetime.now(UTC) - iter_start).total_seconds() * 1000
                )
                _LOGGER.info(
                    "AgentLoop iteration %d/%d: model call took %dms, "
                    "finish=%s, tool_calls=%d, text_len=%d",
                    state.iteration_no, self._max_iterations,
                    iter_latency,
                    response.finish_reason.value,
                    len(response.tool_calls),
                    len(response.text),
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

            # 将 assistant 响应加入消息历史
            assistant_msg = self._make_assistant_message(response)
            state.messages.append(assistant_msg)

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
                # 执行工具调用
                if not self._executor:
                    return self._make_result(
                        LoopResultType.error, state,
                        "Tool calls not supported: no executor configured",
                        finish_reason=FinishReason.error,
                    )
                tool_start = datetime.now(UTC)
                loop_detected = await self._execute_tool_calls(state, response)
                tool_latency = int(
                    (datetime.now(UTC) - tool_start).total_seconds() * 1000
                )
                _LOGGER.info(
                    "AgentLoop iteration %d: tool execution took %dms, "
                    "tool_call_count=%d",
                    state.iteration_no, tool_latency, state.tool_call_count,
                )
                if loop_detected:
                    return self._make_result(
                        LoopResultType.repetition, state,
                        "Loop detected: repeated tool call signature",
                    )
                continue  # 继续下一轮迭代
            elif result == "_invalid":
                if output_repaired:
                    return self._make_result(
                        LoopResultType.invalid_output, state,
                        "Invalid output after repair attempt",
                    )
                # 修复一次：从消息中移除无效的 assistant 回复，重新请求
                output_repaired = True
                state.messages.pop()  # 移除无效回复
                continue

            # fallback
            return self._make_result(LoopResultType.error, state,
                                     f"Unknown response type: {result}")

    def _build_request(
        self, state: LoopState, context: ContextSnapshot,
    ) -> ModelRequest:
        """从 ContextSnapshot、LoopState 和 Registry 构建 ModelRequest。

        注入系统提示词、历史消息和可用工具 Schema。
        """
        messages = []
        for item in context.items:
            role = "system" if item.item_type == "system_policy" else "user"
            messages.append({
                "role": role,
                "content": item.content,
            })
        # 添加历史消息（含 tool 结果）
        messages.extend(state.messages)

        # 注入工具 Schema
        tools = ()
        if self._registry and self._toolsets:
            tools = tuple(self._registry.get_openai_schemas(self._toolsets))

        return ModelRequest(
            messages=messages,
            tools=tools,
            stream=False,
        )

    def _make_assistant_message(
        self, response: ModelResponse,
    ) -> dict[str, Any]:
        """将模型回复格式化为 assistant 消息。"""
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": response.text,
        }
        if response.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in response.tool_calls
            ]
        return msg

    async def _execute_tool_calls(
        self, state: LoopState, response: ModelResponse,
    ) -> bool:
        """执行模型返回的 Tool Calls。

        AGENT-LOOP / 4. Tool Call：Registry resolve → Policy → Approve → Execute → Result → next.
        AGENT-LOOP / 5. 输出校验：无效参数最多修复一次。

        返回 True 表示检测到循环（重复签名），应终止。
        """
        if not self._executor:
            return False

        tool_calls = response.tool_calls
        if not tool_calls:
            return False

        # 本轮已完成 ID 集合（防止同批次内重复）
        round_completed: set[str] = set()

        for tc in tool_calls:
            tool_call_id = tc.get("id", "")
            tool_name = tc["function"]["name"]
            raw_arguments = tc["function"]["arguments"]

            # 本轮内跳过已完成的
            if tool_call_id in round_completed:
                continue
            round_completed.add(tool_call_id)

            # 反序列化参数
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                arguments = {}

            # 签名检测（重复调用保护）
            sig = (tool_name, self._canonical_args(raw_arguments))
            state.tool_signatures.append(sig)

            # 检查是否超过重复阈值
            consecutive = 1
            for i in range(len(state.tool_signatures) - 2, -1, -1):
                if state.tool_signatures[i] == sig:
                    consecutive += 1
                else:
                    break
            if consecutive >= self._max_repeated_tool_signature:
                return True  # loop detected

            # 执行工具
            ctx = ToolContext(
                attempt_id=state.attempt_id,
                trace_id=state.turn_id,
                tool_call_id=tool_call_id,
            )

            # AGENT-LOOP / 7: 副作用工具执行前写 checkpoint
            self._write_checkpoint(state)

            result = await self._executor.execute(
                tool_call_id, tool_name, arguments, ctx,
            )

            # ── 无效参数修复（AGENT-LOOP / 5.1-5.3）──
            if (
                result.status == "error"
                and "validation" in result.error_message.lower()
                and not state.tool_repaired
            ):
                state.tool_repaired = True
                # 发送修复提示，让模型修正参数重试
                state.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": (
                        f"Parameter validation error for tool '{tool_name}': "
                        f"{result.error_message}. Please fix the parameters and retry."
                    ),
                })
                # 不增加 tool_call_count 计数，不注入 error ToolResult
                continue

            # ── 正常处理 ──
            state.tool_call_count += 1

            # 格式化 tool result 消息
            tool_msg = ToolExecutor.format_tool_message(tool_call_id, result)
            state.messages.append(tool_msg)

        return False

    @staticmethod
    def _canonical_args(raw_arguments: str) -> str:
        """对参数做规范化，用于签名比较。"""
        try:
            obj = json.loads(raw_arguments)
            return hashlib.md5(json.dumps(obj, sort_keys=True).encode()).hexdigest()
        except (json.JSONDecodeError, TypeError):
            return raw_arguments

    def _write_checkpoint(self, state: LoopState) -> None:
        """将当前 LoopState 写入 checkpoint（AGENT-LOOP / 7）。"""
        if not self._checkpoint_callback:
            return
        try:
            self._checkpoint_callback({
                "turn_id": state.turn_id,
                "iteration_no": state.iteration_no,
                "tool_call_count": state.tool_call_count,
                "message_count": len(state.messages),
                "last_signature": (
                    state.tool_signatures[-1] if state.tool_signatures else None
                ),
            })
        except Exception:
            pass  # checkpoint 失败不阻断主流程

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
            tool_call_count=state.tool_call_count,
            finish_reason=finish_reason,
        )
