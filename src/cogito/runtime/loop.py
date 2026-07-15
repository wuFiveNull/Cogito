"""Agent Loop — 模型执行循环。

AGENT-LOOP / 2. LoopState：循环内状态和可恢复字段。
AGENT-LOOP / 3. 单轮协议：统一 ModelResponse 输出类型。
AGENT-LOOP / 4. Tool Call：执行并迭代。
AGENT-LOOP / 5. 输出校验与修复：无效输出最多修复一次。
AGENT-LOOP / 6. 终止条件。
AGENT-LOOP / 11. 验收测试。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from cogito.capability.executor import ToolExecutor
from cogito.capability.models import ToolContext
from cogito.capability.registry import CapabilityRegistry
from cogito.contracts.context import ContextSnapshot
from cogito.model.contracts import (
    ContentPart,
    FinishReason,
    ModelRequest,
    ModelResponse,
    Usage,
)
from cogito.model.router import ModelRouter, RouterError

_LOGGER = logging.getLogger(__name__)


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
    waiting_approval = "waiting_approval"
    waiting_external = "waiting_external"


@dataclass(frozen=True)
class LoopResult:
    """Agent Loop 的最终输出。"""

    result_type: LoopResultType = LoopResultType.final_response
    content_parts: tuple[ContentPart, ...] = ()
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    iterations: int = 0
    model_call_count: int = 0
    tool_call_count: int = 0
    error_message: str = ""
    finish_reason: FinishReason = FinishReason.stop
    approval_id: str = ""
    waiting_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_parts", tuple(self.content_parts))

    @property
    def is_success(self) -> bool:
        return self.result_type == LoopResultType.final_response


@dataclass(frozen=True)
class ResourceBudget:
    """Agent Loop 统一硬约束 (Plan 02 M4)。

    跨 Attempt 恢复：Budget 从 Checkpoint 恢复，不归零。
    限制至少覆盖：max_loop_iterations / max_model_calls / max_tool_calls /
    max_input_tokens / max_output_tokens / max_wall_time / max_cost。
    """

    max_loop_iterations: int = 10
    max_model_calls: int = 20
    max_tool_calls: int = 50
    max_input_tokens: int = 32000
    max_output_tokens: int = 8192
    max_wall_time_s: int = 120
    max_cost: float = 0.0  # 0 = 不限

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_loop_iterations": self.max_loop_iterations,
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_wall_time_s": self.max_wall_time_s,
            "max_cost": self.max_cost,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceBudget:
        return cls(
            **{
                k: data.get(k, def_v)
                for k, def_v in {
                    "max_loop_iterations": 10,
                    "max_model_calls": 20,
                    "max_tool_calls": 50,
                    "max_input_tokens": 32000,
                    "max_output_tokens": 8192,
                    "max_wall_time_s": 120,
                    "max_cost": 0.0,
                }.items()
            }
        )


@dataclass
class LoopState:
    """Agent Loop 运行时状态。

    明确记录轮次、模型调用数、Tool 调用数、重复签名和剩余预算。
    """

    turn_id: str = ""
    attempt_id: str = ""
    iteration_no: int = 0
    model_call_count: int = 0
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
    # Budget (跨 Attempt 恢复)
    budget: ResourceBudget = field(default_factory=ResourceBudget)
    accumulated_cost: float = 0.0
    output_repaired: bool = False  # 结构化输出失败只允许预算内修复一次
    pending_approval_id: str = ""
    pending_external_id: str = ""
    exposed_tools: set[str] = field(default_factory=set)
    tool_state: dict[str, Any] = field(default_factory=dict)
    capability_schemas: list[dict[str, Any]] = field(default_factory=list)


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
        budget: ResourceBudget | None = None,
        checkpoint_callback: Callable[[dict], None] | None = None,
        checkpoint_loader: Callable[[str], dict[str, Any] | None] | None = None,
        agent_mode: str = "reactive",
        policy_allowed_capabilities: set[str] | None = None,
    ) -> None:
        self._router = router
        self._registry = registry
        self._executor = executor
        self._toolsets = toolsets or set()
        # ResourceBudget 优先；否则从 legacy 参数构造
        self._budget = budget or ResourceBudget(
            max_loop_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
            max_wall_time_s=max_runtime_s,
            max_input_tokens=max_total_tokens,
        )
        self._max_repeated_tool_signature = max_repeated_tool_signature
        self._checkpoint_callback = checkpoint_callback
        self._checkpoint_loader = checkpoint_loader
        self._agent_mode = agent_mode
        self._policy_allowed_capabilities = policy_allowed_capabilities

    async def run(
        self,
        context: ContextSnapshot,
        model_role: str = "main",
        cancel_flag: Callable[[], bool] | None = None,
    ) -> LoopResult:
        """执行 Agent Loop。"""
        state = LoopState(
            turn_id=context.turn_id,
            attempt_id=getattr(context, "attempt_id", None) or "",
            context_snapshot_id=context.snapshot_id,
            started_at=datetime.now(UTC),
            iteration_no=0,
            model_call_count=0,
            budget=self._budget,
        )
        if self._registry is not None:
            snapshot = self._registry.build_snapshot(
                mode=self._agent_mode,
                toolsets=self._toolsets,
                policy_allowed=self._policy_allowed_capabilities,
            )
            state.capability_schemas = [
                {
                    "capability_id": tool.capability_id,
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "deferred": tool.deferred,
                }
                for tool in snapshot.capabilities
            ]
        if self._checkpoint_loader:
            try:
                saved = self._checkpoint_loader(context.turn_id) or {}
                state.iteration_no = int(saved.get("iteration_no", 0))
                state.model_call_count = int(saved.get("model_call_count", 0))
                state.tool_call_count = int(saved.get("tool_call_count", 0))
                saved_usage = dict(saved.get("usage", {}))
                state.usage = Usage(
                    input_tokens=int(saved_usage.get("input_tokens", 0)),
                    output_tokens=int(saved_usage.get("output_tokens", 0)),
                    cached_tokens=int(saved_usage.get("cached_tokens", 0)),
                )
                state.accumulated_cost = float(saved.get("accumulated_cost", 0.0))
                elapsed = max(0.0, float(saved.get("elapsed_wall_seconds", 0.0)))
                state.started_at = datetime.now(UTC) - timedelta(seconds=elapsed)
                if saved.get("budget"):
                    # A checkpoint may only preserve or tighten the original budget.
                    restored = ResourceBudget.from_dict(dict(saved["budget"]))
                    state.budget = ResourceBudget(
                        **{
                            key: min(getattr(self._budget, key), getattr(restored, key))
                            if key != "max_cost" or self._budget.max_cost > 0
                            else restored.max_cost
                            for key in self._budget.to_dict()
                        }
                    )
                state.messages = list(saved.get("messages", []))
                state.exposed_tools = set(saved.get("exposed_tools", []))
                state.tool_state = dict(saved.get("tool_state", {}))
                if saved.get("capability_schemas"):
                    state.capability_schemas = list(saved["capability_schemas"])
                state.pending_external_id = ""
            except Exception:
                pass

        # A resumed Turn consumes and executes the exact approved Tool call
        # before asking the model to continue.
        if self._executor is not None:
            resume_ctx = self._make_tool_context(state, context, "approved-resume")
            approved = await self._executor.resume_approved(resume_ctx)
            if approved is not None:
                has_original_call = any(
                    any(
                        call.get("id") == approved.tool_call_id
                        for call in message.get("tool_calls", [])
                    )
                    for message in state.messages
                    if message.get("role") == "assistant"
                )
                if not has_original_call:
                    state.messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": approved.tool_call_id,
                                    "type": "function",
                                    "function": {"name": approved.tool_name, "arguments": "{}"},
                                }
                            ],
                        }
                    )
                state.messages.append(
                    ToolExecutor.format_tool_message(
                        approved.tool_call_id,
                        approved,
                    )
                )
                state.tool_call_count += 1
            deferred = self._executor.claim_deferred_result(resume_ctx)
            if deferred is not None:
                state.messages.append(
                    ToolExecutor.format_tool_message(deferred.tool_call_id, deferred),
                )
                state.tool_call_count += 1

        while True:
            state.iteration_no += 1
            state.last_iteration_at = datetime.now(UTC)

            # ── 检查取消 ──
            if cancel_flag and cancel_flag():
                return self._make_result(LoopResultType.cancelled, state, "Cancelled by request")

            # ── 终止条件检查（迭代次数）──
            if state.iteration_no > state.budget.max_loop_iterations:
                return self._make_result(
                    LoopResultType.max_iterations,
                    state,
                    f"Exceeded max iterations ({state.budget.max_loop_iterations})",
                )

            # ── 终止条件检查（模型调用数）──
            if state.model_call_count >= state.budget.max_model_calls:
                return self._make_result(
                    LoopResultType.max_iterations,
                    state,
                    f"Exceeded max model calls ({state.budget.max_model_calls})",
                )

            # ── 终止条件检查（Tool 调用次数）──
            if state.tool_call_count >= state.budget.max_tool_calls:
                return self._make_result(
                    LoopResultType.max_tool_calls,
                    state,
                    f"Exceeded max tool calls ({state.budget.max_tool_calls})",
                )

            # ── 终止条件检查（运行时间）──
            elapsed = state.last_iteration_at - state.started_at
            if elapsed > timedelta(seconds=state.budget.max_wall_time_s):
                return self._make_result(
                    LoopResultType.max_runtime,
                    state,
                    f"Exceeded max runtime ({state.budget.max_wall_time_s}s)",
                )

            # ── 终止条件检查（总 Token）──
            if state.usage.total_tokens > state.budget.max_input_tokens:
                return self._make_result(
                    LoopResultType.max_tokens,
                    state,
                    f"Exceeded max tokens ({state.budget.max_input_tokens})",
                )

            # ── 构建 ModelRequest ──
            request = self._build_request(state, context)

            # ── 调用 Provider ──
            try:
                state.model_call_count += 1
                iter_start = datetime.now(UTC)
                response = await self._router.generate(
                    request,
                    model_role=model_role,
                )
                iter_latency = int((datetime.now(UTC) - iter_start).total_seconds() * 1000)
                _LOGGER.info(
                    "AgentLoop iteration %d/%d (model_call #%d): model call took %dms, "
                    "finish=%s, tool_calls=%d, text_len=%d",
                    state.iteration_no,
                    state.budget.max_loop_iterations,
                    state.model_call_count,
                    iter_latency,
                    response.finish_reason.value,
                    len(response.tool_calls),
                    len(response.text),
                )
            except RouterError as e:
                return self._make_result(
                    LoopResultType.error,
                    state,
                    f"Provider error: {e}",
                    finish_reason=FinishReason.error,
                )

            # 累计 Usage
            state.usage = state.usage + response.usage
            state.total_latency_ms += iter_latency

            if (
                state.usage.total_tokens > state.budget.max_input_tokens
                or state.usage.output_tokens > state.budget.max_output_tokens
            ):
                return self._make_result(
                    LoopResultType.max_tokens,
                    state,
                    "Model response exceeded the remaining token budget",
                )

            # 将 assistant 响应加入消息历史
            assistant_msg = self._make_assistant_message(response)
            state.messages.append(assistant_msg)

            # ── 检查响应 ──
            result = self._classify_response(response)

            if result == "_final":
                return self._make_result(
                    LoopResultType.final_response,
                    state,
                    response.text,
                    content_parts=list(response.content_parts),
                    usage=state.usage,
                    finish_reason=response.finish_reason,
                )
            elif result == "_refusal":
                return self._make_result(
                    LoopResultType.refusal,
                    state,
                    response.text,
                    content_parts=list(response.content_parts),
                )
            elif result == "_tool_call":
                # 执行工具调用
                if not self._executor:
                    return self._make_result(
                        LoopResultType.error,
                        state,
                        "Tool calls not supported: no executor configured",
                        finish_reason=FinishReason.error,
                    )
                tool_start = datetime.now(UTC)
                loop_detected = await self._execute_tool_calls(state, response, context)
                tool_latency = int((datetime.now(UTC) - tool_start).total_seconds() * 1000)
                _LOGGER.info(
                    "AgentLoop iteration %d: tool execution took %dms, tool_call_count=%d",
                    state.iteration_no,
                    tool_latency,
                    state.tool_call_count,
                )
                if loop_detected:
                    return self._make_result(
                        LoopResultType.repetition,
                        state,
                        "Loop detected: repeated tool call signature",
                    )
                if state.pending_approval_id:
                    return self._make_result(
                        LoopResultType.waiting_approval,
                        state,
                        "Tool call is waiting for approval",
                        approval_id=state.pending_approval_id,
                    )
                if state.pending_external_id:
                    return self._make_result(
                        LoopResultType.waiting_external,
                        state,
                        "Tool call is waiting for external work",
                        waiting_id=state.pending_external_id,
                    )
                continue  # 继续下一轮迭代
            elif result == "_invalid":
                if state.output_repaired:
                    return self._make_result(
                        LoopResultType.invalid_output,
                        state,
                        "Invalid output after repair attempt",
                    )
                # 结构化输出失败只允许预算内修复一次（AGENT-LOOP / 5）
                state.output_repaired = True
                state.messages.pop()  # 移除无效回复
                continue

            # fallback
            return self._make_result(
                LoopResultType.error, state, f"Unknown response type: {result}"
            )

    async def run_stream(
        self,
        context: ContextSnapshot,
        model_role: str = "main",
        cancel_flag: Callable[[], bool] | None = None,
    ) -> AsyncIterator[tuple[str, bool]]:
        """流式执行 Agent Loop —— yield (delta_text, is_segment_end)。

        复刻 run() 的迭代/终止/工具调用逻辑：
        - 工具迭代、修复迭代等非最终段：用 generate（非流式），不 yield 文本；
        - 最终自然语言段：复用上方判定调用已返回的全文，切片后逐步 yield
          （整段仅一次模型调用，消除「先判定再流式」的第二次前向），
          yield 每个 delta (文本, False)，末帧后 yield ("", True) 表示本段结束。
        若 cancel_flag() 在 yield 前为真 → 关闭生成器（控制器负责 withdraw）。
        """
        state = LoopState(
            turn_id=context.turn_id,
            attempt_id=getattr(context, "attempt_id", None) or "",
            context_snapshot_id=context.snapshot_id,
            started_at=datetime.now(UTC),
            iteration_no=0,
            budget=self._budget,
        )
        output_repaired = False

        while True:
            state.iteration_no += 1
            state.last_iteration_at = datetime.now(UTC)

            # 取消检查
            if cancel_flag and cancel_flag():
                return

            # 终止条件检查（使用 ResourceBudget 统一硬约束）
            if state.iteration_no > state.budget.max_loop_iterations:
                return
            if state.model_call_count >= state.budget.max_model_calls:
                return
            if state.tool_call_count >= state.budget.max_tool_calls:
                return
            elapsed = state.last_iteration_at - state.started_at
            if elapsed > timedelta(seconds=state.budget.max_wall_time_s):
                return
            if state.usage.total_tokens > state.budget.max_input_tokens:
                return

            # 构建请求并调用 Provider（判定用，非流式）
            request = self._build_request(state, context, stream=False)
            try:
                iter_start = datetime.now(UTC)
                response = await self._router.generate(request, model_role=model_role)
                iter_latency = int((datetime.now(UTC) - iter_start).total_seconds() * 1000)
            except RouterError:
                return  # 错误由控制器外层 try 捕获

            state.usage = state.usage + response.usage
            state.total_latency_ms += iter_latency
            if (
                state.usage.total_tokens > state.budget.max_input_tokens
                or state.usage.output_tokens > state.budget.max_output_tokens
            ):
                return

            result = self._classify_response(response)

            if result in ("_final", "_refusal"):
                # 最终自然语言段：复用上方判定调用已返回的全文，切片后逐步 yield，
                # 避免「先判定再流式」的第二次模型调用（整段仅一次模型调用）。
                final_text = response.text
                accumulated: list[str] = []
                for piece in self._chunk_for_stream(final_text):
                    if cancel_flag and cancel_flag():
                        return
                    accumulated.append(piece)
                    yield (piece, False)
                state.messages.append({"role": "assistant", "content": final_text})
                yield ("", True)  # 段结束哨兵
                return

            elif result == "_tool_call":
                if not self._executor:
                    return
                state.messages.append(self._make_assistant_message(response))
                tool_start = datetime.now(UTC)
                loop_detected = await self._execute_tool_calls(state, response, context)
                _ = int((datetime.now(UTC) - tool_start).total_seconds() * 1000)
                if loop_detected:
                    return
                if state.pending_approval_id:
                    return
                continue

            elif result == "_invalid":
                if output_repaired:
                    return
                output_repaired = True
                state.messages.append(self._make_assistant_message(response))
                state.messages.pop()  # 移除无效回复
                continue

            else:
                return

    def _build_request(
        self,
        state: LoopState,
        context: ContextSnapshot,
        stream: bool = False,
    ) -> ModelRequest:
        """从 ContextSnapshot、LoopState 和 Registry 构建 ModelRequest。

        注入系统提示词、历史消息和可用工具 Schema。
        stream=True 时启用 SSE 流式（仅最终自然语言段使用）。
        """
        messages = []
        for item in context.items:
            # 使用 ContextItem 中保留的原始 role，不乱映射
            role = item.role or ("system" if item.item_type == "system_policy" else "user")
            messages.append(
                {
                    "role": role,
                    "content": item.content,
                }
            )
        # 添加历史消息（含 tool 结果）
        messages.extend(state.messages)

        # 注入工具 Schema
        tools = ()
        if state.capability_schemas:
            tools = tuple(
                {
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "description": str(item.get("description", ""))[:512],
                        "parameters": item["parameters"],
                    },
                }
                for item in state.capability_schemas
                if not item.get("deferred")
                or (
                    item["name"] in state.exposed_tools
                    or item["capability_id"] in state.exposed_tools
                )
            )

        return ModelRequest(
            messages=messages,
            tools=tools,
            max_output_tokens=max(
                1,
                state.budget.max_output_tokens - state.usage.output_tokens,
            ),
            timeout=timedelta(
                seconds=max(
                    1,
                    state.budget.max_wall_time_s
                    - int(
                        (
                            (state.last_iteration_at or datetime.now(UTC))
                            - (state.started_at or datetime.now(UTC))
                        ).total_seconds()
                    ),
                ),
            ),
            stream=stream,
        )

    def _make_assistant_message(
        self,
        response: ModelResponse,
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
        self,
        state: LoopState,
        response: ModelResponse,
        context: ContextSnapshot,
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
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                )
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
            ctx = self._make_tool_context(state, context, tool_call_id)

            # AGENT-LOOP / 7: 副作用工具执行前写 checkpoint
            self._write_checkpoint(state)

            result = await self._executor.execute(
                tool_call_id,
                tool_name,
                arguments,
                ctx,
            )

            if result.status == "approval_required":
                state.pending_approval_id = result.approval_id
                self._write_checkpoint(state)
                return False
            if result.status == "waiting_external":
                state.pending_external_id = result.waiting_id
                self._write_checkpoint(state)
                return False

            # ── 无效参数修复（AGENT-LOOP / 5.1-5.3）──
            if (
                result.status == "error"
                and "validation" in result.error_message.lower()
                and not state.tool_repaired
            ):
                state.tool_repaired = True
                # 发送修复提示，让模型修正参数重试
                state.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": (
                            f"Parameter validation error for tool '{tool_name}': "
                            f"{result.error_message}. Please fix the parameters and retry."
                        ),
                    }
                )
                # 不增加 tool_call_count 计数，不注入 error ToolResult
                continue

            # ── 正常处理 ──
            state.tool_call_count += 1

            # 格式化 tool result 消息
            tool_msg = ToolExecutor.format_tool_message(tool_call_id, result)
            state.messages.append(tool_msg)

        return False

    def _make_tool_context(
        self,
        state: LoopState,
        context: ContextSnapshot,
        tool_call_id: str,
    ) -> ToolContext:
        return ToolContext(
            attempt_id=state.attempt_id,
            trace_id=state.turn_id,
            tool_call_id=tool_call_id,
            principal_id=context.principal_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            input_message_id=context.input_message_id,
            conversation_id=context.conversation_id,
            agent_mode=self._agent_mode,
            user_request=self._latest_user_request(state.messages),
            expose_tool=lambda name: self._expose_tool(state, name),
            tool_state=state.tool_state,
            allowed_toolsets=tuple(sorted(self._toolsets)),
            capability_snapshot_ids=tuple(
                item["capability_id"] for item in state.capability_schemas
            ),
            resource_budget=state.budget.to_dict(),
            resource_usage={
                "loop_iterations": state.iteration_no,
                "model_calls": state.model_call_count,
                "tool_calls": state.tool_call_count,
                "input_tokens": state.usage.input_tokens,
                "output_tokens": state.usage.output_tokens,
                "total_tokens": state.usage.total_tokens,
                "cost": state.accumulated_cost,
                "wall_time_s": max(
                    0,
                    int(
                        (
                            (state.last_iteration_at or datetime.now(UTC))
                            - (state.started_at or datetime.now(UTC))
                        ).total_seconds()
                    ),
                ),
            },
        )

    def _expose_tool(self, state: LoopState, name: str) -> bool:
        if self._registry is None:
            return False
        tool = self._registry.get(name)
        snapshot_ids = {item["capability_id"] for item in state.capability_schemas}
        if (
            tool is None
            or tool.capability_id not in snapshot_ids
            or not (set(tool.toolset) & self._toolsets)
        ):
            return False
        state.exposed_tools.add(tool.capability_id)
        state.exposed_tools.add(tool.name)
        return True

    @staticmethod
    def _latest_user_request(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            return json.dumps(content, ensure_ascii=False)
        return ""

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
            self._checkpoint_callback(
                {
                    "turn_id": state.turn_id,
                    "iteration_no": state.iteration_no,
                    "model_call_count": state.model_call_count,
                    "tool_call_count": state.tool_call_count,
                    "usage": {
                        "input_tokens": state.usage.input_tokens,
                        "output_tokens": state.usage.output_tokens,
                        "cached_tokens": state.usage.cached_tokens,
                    },
                    "accumulated_cost": state.accumulated_cost,
                    "elapsed_wall_seconds": (
                        (datetime.now(UTC) - state.started_at).total_seconds()
                        if state.started_at
                        else 0.0
                    ),
                    "budget": state.budget.to_dict(),
                    "message_count": len(state.messages),
                    "last_signature": (
                        state.tool_signatures[-1] if state.tool_signatures else None
                    ),
                    "pending_approval_id": state.pending_approval_id,
                    "pending_external_id": state.pending_external_id,
                    "messages": state.messages,
                    "exposed_tools": sorted(state.exposed_tools),
                    "tool_state": state.tool_state,
                    "capability_schemas": state.capability_schemas,
                }
            )
        except Exception:
            pass  # checkpoint 失败不阻断主流程

    @staticmethod
    def _chunk_for_stream(text: str) -> Iterator[str]:
        """将最终段全文切成小块逐步 yield，模拟 token 流式输出。

        注意：run_stream 在最终段复用判定调用的全文（只发了一次模型请求），
        这里只是把已拿到的结果按字符切片逐步吐出，制造流式视觉效果；
        真正的首字延迟来自上方的单次模型调用，而非此处切分。
        中文按 3 字一块、英文按词整体切分，兼顾两种语料。
        """
        import re

        cjk = "\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
        # 每个 CJK 字符单独成 token；连续 ASCII 词整体；空白段整体；其他单字符
        tokens = re.findall(rf"[{cjk}]|[A-Za-z0-9_]+|\s+|[^\s]", text)
        buf: list[str] = []
        cjk_count = 0
        for tok in tokens:
            buf.append(tok)
            if len(tok) == 1 and cjk[0] <= tok <= cjk[-1]:
                cjk_count += 1
            else:
                cjk_count = 0
            latin_word = len(tok) > 1 and not (cjk[0] <= tok[0] <= cjk[-1])
            if cjk_count >= 3 or latin_word:
                yield "".join(buf)
                buf = []
                cjk_count = 0
        if buf:
            yield "".join(buf)

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
        approval_id: str = "",
        waiting_id: str = "",
    ) -> LoopResult:
        return LoopResult(
            result_type=result_type,
            content_parts=tuple(content_parts or [ContentPart(text=text)]),
            text=text,
            usage=usage or state.usage,
            latency_ms=state.total_latency_ms,
            iterations=state.iteration_no,
            model_call_count=state.model_call_count,
            tool_call_count=state.tool_call_count,
            finish_reason=finish_reason,
            approval_id=approval_id,
            waiting_id=waiting_id,
        )
