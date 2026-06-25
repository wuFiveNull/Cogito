# cogito/agent/runtime/phases/agent_loop.py
#
# AgentLoopPhase — Phase 5 of the 8-phase pipeline.
#
# Responsible for the "inference ↔ action" loop:
#   Model Call → Tool Calls → Tool Results → Model Call → … → Final Text
#
# Design rules (see agent-loop-spec §10, §22):
#   - Stateless Phase — all per-turn state goes into TurnContext.
#   - Only depends on abstract Ports (no model SDK, no tool impl).
#   - ModelPort is stream-only (non-streaming models wrapped by adapter).
#   - Tool calls are fully prepared before any execution begins.
#   - Policy decision on the whole batch before any tool runs.
#   - Tool results are appended in model-declared ordinal order.
#   - Asyncio.CancelledError propagates unwrapped.
#   - All persistent state is written through TurnContext only.

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Mapping

from cogito.agent.domain.approval import (
    AgentLoopCheckpoint,
    ApprovalAction,
    PendingApprovalBatch,
    PendingApprovalItem,
)
from cogito.agent.domain.messages import (
    AssistantMessage,
    ModelMessage,
    ToolMessage,
    UserMessage,
)
from cogito.agent.domain.model import (
    ModelFinishReason,
    ModelInvocationRequest,
    ModelRoundOutput,
    ModelStreamEvent,
    ModelTextDelta,
    ModelToolCallDelta,
)
from cogito.agent.domain.tools import (
    PreparedToolCall,
    RejectedToolCall,
    ToolCall,
    ToolCallPlan,
    ToolDefinition,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolSideEffect,
)
from cogito.agent.domain.usage import ToolExecutionRecord, UsageSummary
from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.model import ModelPort
from cogito.agent.ports.model_context import (
    ContextWindowRequest,
    ModelContextWindowPort,
)
from cogito.agent.ports.tool_policy import (
    ToolPolicyDecision,
    ToolPolicyDecisionType,
    ToolPolicyPort,
)
from cogito.agent.ports.tools import (
    ToolExecutionContext,
    ToolExecutorPort,
    ToolRegistryPort,
)
from cogito.agent.runtime.agent_loop.assembler import ModelResponseAssembler
from cogito.agent.runtime.agent_loop.loop_guard import ToolLoopGuard, ToolLoopGuardConfig
from cogito.agent.runtime.agent_loop.usage_accumulator import UsageAccumulator
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalExpiredError,
    ContextWindowExceededError,
    DuplicateToolCallIdError,
    InvalidAgentLoopStateError,
    InvalidApprovalCheckpointError,
    MaxModelCallsExceededError,
    MaxToolCallsPerRoundExceededError,
    MaxTotalToolCallsExceededError,
    ModelInvocationError,
    ModelInvocationTimeoutError,
    ToolCallTimeoutError,
    TurnDeadlineExceededError,
)
from cogito.agent.runtime.history_hardening import harden_messages
from cogito.agent.runtime.agent_loop.governance import (
    backfill_missing_tool_results,
    degrade_tool_results,
    drop_orphan_tool_pairs,
    estimate_messages_tokens,
    snip_history,
)
from cogito.agent.ports.model import ModelPort
from cogito.agent.ports.summarizer import SummarizerPort
from cogito.agent.runtime.events import AgentEventType
from cogito.agent.runtime.models import TurnStatus
from cogito.agent.runtime.phase import BasePhase

logger = logging.getLogger(__name__)


# ── Configuration ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    """Immutable configuration for AgentLoopPhase.

    All values must be positive.  max_tool_rounds and max_total_tool_calls
    are the effective upper bounds; Turn-level override (from TurnInit or
    session config) takes the more restrictive of system vs turn.
    """

    model_call_timeout_seconds: float = 90.0
    default_tool_timeout_seconds: float = 30.0
    max_tool_rounds: int = 8
    max_total_tool_calls: int = 24
    max_tool_calls_per_round: int = 8
    max_parallel_tools: int = 4
    max_output_tokens: int = 4_096
    max_repeated_fingerprint: int = 2
    cycle_detection_window: int = 8
    max_model_text_chars: int = 200_000
    approval_message: str = "该操作需要确认后才能继续。"

    # ── History hardening ───────────────────────────────────────────
    enable_history_hardening: bool = True

    # ── Runtime governance ──────────────────────────────────────────
    enable_runtime_governance: bool = True
    tool_result_pruning_enabled: bool = False
    tool_result_keep_recent_n: int = 3
    tool_result_max_old_chars: int = 500
    enable_hard_limit: bool = False
    runtime_hard_limit_tokens: int = 28_416

    # ── Retry / anti-thrashing ──────────────────────────────────────
    enable_retry_on_overflow: bool = True
    anti_thrashing_threshold: float = 0.10
    anti_thrashing_max_ineffective: int = 2

    # ── Iterative summarization (Phase 3, Mode 4) ──────────────────
    enable_iterative_summarization: bool = False
    summarization_max_history_chars: int = 8_000
    summarization_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not all(
            v > 0
            for v in (
                self.model_call_timeout_seconds,
                self.default_tool_timeout_seconds,
                self.max_tool_rounds,
                self.max_total_tool_calls,
                self.max_tool_calls_per_round,
                self.max_parallel_tools,
                self.max_output_tokens,
                self.max_repeated_fingerprint,
                self.cycle_detection_window,
                self.max_model_text_chars,
                self.runtime_hard_limit_tokens,
            )
        ):
            raise ValueError("AgentLoopConfig values must be positive")


# ── AgentLoopPhase ──────────────────────────────────────────────────────


class AgentLoopPhase(BasePhase):
    """Phase 5: Model-tool loop producing the final agent response."""

    name = "agent_loop"

    def __init__(
        self,
        *,
        model: ModelPort,
        context_window: ModelContextWindowPort,
        tool_registry: ToolRegistryPort,
        tool_policy: ToolPolicyPort,
        tool_executor: ToolExecutorPort,
        clock: ClockPort,
        config: AgentLoopConfig,
        assembler_factory: type[ModelResponseAssembler] = ModelResponseAssembler,
        loop_guard_factory: type[ToolLoopGuard] = ToolLoopGuard,
        loop_guard_config: ToolLoopGuardConfig | None = None,
        summarizer: SummarizerPort | None = None,
    ) -> None:
        self._model = model
        self._context_window = context_window
        self._tool_registry = tool_registry
        self._tool_policy = tool_policy
        self._tool_executor = tool_executor
        self._clock = clock
        self._config = config
        self._assembler_factory = assembler_factory
        self._loop_guard_factory = loop_guard_factory
        self._loop_guard_config = loop_guard_config or ToolLoopGuardConfig()
        self._summarizer = summarizer

    # ══════════════════════════════════════════════════════════════════
    # Main entry point
    # ══════════════════════════════════════════════════════════════════

    async def execute(self, ctx: TurnContext) -> None:
        """Run the model-tool loop until a final response or suspension."""
        self._validate_entry_state(ctx)

        loop_guard = self._loop_guard_factory(
            max_repeated_fingerprint=self._config.max_repeated_fingerprint,
            cycle_detection_window=self._config.cycle_detection_window,
        )

        # Resume from a previous approval checkpoint if needed
        if ctx.loop_checkpoint is not None:
            await self._resume_from_checkpoint(ctx, loop_guard)
            if ctx.status is not None:
                # Suspended again or completed — exit
                return

        usage_acc = UsageAccumulator()
        loop_breaker_delay = 0.0  # rate-limit placeholder

        while ctx.output_text is None:
            self._check_cancellation_and_deadline(ctx)
            self._check_model_budget(ctx)

            # ── Runtime governance ────────────────────────────────────
            await self._govern_context(ctx)

            # ── Build request + invoke with retry loop ────────────────
            request = await self._build_model_request(ctx)

            try:
                output = await self._invoke_model(ctx, request)
            except ContextWindowExceededError as _cwe:
                if not self._config.enable_retry_on_overflow:
                    raise
                retried = await self._handle_context_overflow(ctx)
                if not retried:
                    raise
                continue  # retry with degraded context
            output = await self._invoke_model(ctx, request)

            # Record the round
            ctx.model_calls_used += 1
            ctx.model_responses.append(output)
            usage_acc.add_model_round(output)

            # Final response — text only, no tools
            if output.text is not None and not output.tool_calls:
                final_msg = AssistantMessage(
                    content=output.text,
                    provider_response_id=output.provider_response_id,
                )
                ctx.model_messages.append(final_msg)
                ctx.final_response = final_msg
                ctx.output_text = output.text
                ctx.usage = usage_acc.snapshot()
                return

            # ── Tool call path (with or without text) ──────────────────

            self._check_tool_budget(ctx, output.tool_calls)
            plan = self._prepare_tool_plan(ctx, output.tool_calls)
            loop_guard.check_batch(plan)

            # Append AssistantMessage; include content when model also
            # produced explanatory text before calling tools.
            ctx.model_messages.append(
                AssistantMessage(
                    content=output.text,  # None for tool-only, str for mixed
                    tool_calls=plan.original_calls,
                    provider_response_id=output.provider_response_id,
                ),
            )

            # Policy evaluation on the entire executable batch
            decisions = await self._evaluate_policy(
                ctx,
                plan.executable_calls,
            )
            if self._requires_approval(decisions):
                await self._suspend_for_approval(ctx, plan, decisions)
                return

            # Execute tools
            results = await self._execute_tool_plan(ctx, plan, decisions)

            # Append ToolMessages in ordinal order
            self._append_tool_results(ctx, plan.original_calls, results)

            # Update counters + loop guard
            ctx.tool_rounds_used += 1
            ctx.total_tool_calls_used += len(plan.original_calls)
            for _ in plan.original_calls:
                usage_acc.add_tool_call()
            loop_guard.record_batch(plan, results)

        # Should never reach here
        raise InvalidAgentLoopStateError(
            "Agent loop exited without an explicit terminal condition",
        )

    # ══════════════════════════════════════════════════════════════════
    # Validation & preconditions
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _validate_entry_state(ctx: TurnContext) -> None:
        if ctx.turn_id is None:
            raise InvalidAgentLoopStateError("turn_id is required")
        if ctx.status is TurnStatus.WAITING_APPROVAL:
            raise InvalidAgentLoopStateError(
                f"cannot start from WAITING_APPROVAL without a checkpoint",
            )
        if ctx.status is not TurnStatus.RUNNING and ctx.loop_checkpoint is None:
            raise InvalidAgentLoopStateError(
                f"turn status must be RUNNING or have a checkpoint, got {ctx.status}",
            )
        if not ctx.model_messages:
            raise InvalidAgentLoopStateError("model_messages must not be empty")
        if ctx.output_text is not None:
            raise InvalidAgentLoopStateError("output_text must be None on entry")

        # Must contain at least one UserMessage
        has_user = any(isinstance(m, UserMessage) for m in ctx.model_messages)
        if not has_user:
            raise InvalidAgentLoopStateError(
                "model_messages must contain at least one UserMessage",
            )

        if ctx.available_tools:
            names = {t.name for t in ctx.available_tools}
            if len(names) != len(ctx.available_tools):
                raise InvalidAgentLoopStateError(
                    "available_tools names must be unique",
                )

    @staticmethod
    def _check_cancellation_and_deadline(ctx: TurnContext) -> None:
        if ctx.cancellation_requested:
            raise asyncio.CancelledError("Turn cancelled")

        token = ctx.cancellation_token
        if token is not None:
            token.raise_if_cancelled()

        deadline = ctx.deadline_at
        if deadline is not None:
            from datetime import datetime

            if datetime.now() >= deadline:
                raise TurnDeadlineExceededError(
                    "Turn deadline exceeded",
                    safe_message="响应超时",
                )

    def _check_model_budget(self, ctx: TurnContext) -> None:
        max_model_calls = self._config.max_tool_rounds + 1
        if ctx.model_calls_used >= max_model_calls:
            raise MaxModelCallsExceededError(
                f"Model calls {ctx.model_calls_used} >= {max_model_calls}",
                safe_message="模型调用次数超限",
            )

    # ══════════════════════════════════════════════════════════════════
    # Model invocation
    # ══════════════════════════════════════════════════════════════════

    async def _build_model_request(self, ctx: TurnContext) -> ModelInvocationRequest:
        """Fit context window, harden history (if enabled), build and return a ModelInvocationRequest."""
        # ── History hardening ──────────────────────────────────────────
        if self._config.enable_history_hardening:
            ctx.model_messages = harden_messages(ctx.model_messages)

        round_index = ctx.model_calls_used
        timeout = self._remaining_call_timeout(ctx)

        # Context window fit
        fitted = await self._context_window.fit(
            ContextWindowRequest(
                messages=tuple(ctx.model_messages),
                tools=tuple(ctx.available_tools),
                reserved_output_tokens=self._config.max_output_tokens,
            ),
        )

        return ModelInvocationRequest(
            turn_id=ctx.turn_id or "",
            request_id=ctx.request.request_id,
            round_index=round_index,
            messages=fitted,
            tools=tuple(ctx.available_tools),
            timeout_seconds=timeout,
            max_output_tokens=self._config.max_output_tokens,
        )

    # ══════════════════════════════════════════════════════════════════
    # Runtime governance
    # ══════════════════════════════════════════════════════════════════

    async def _govern_context(self, ctx: TurnContext) -> None:
        """Run multi-layer context quality control before model invocation.

        Each step is idempotent and gated by config flags.
        """
        if not self._config.enable_runtime_governance:
            return

        # 1. Drop orphaned tool pairs
        ctx.model_messages = drop_orphan_tool_pairs(ctx.model_messages)

        # 2. Backfill missing tool results
        ctx.model_messages = backfill_missing_tool_results(ctx.model_messages)

        # 3. Degrade old tool results
        if self._config.tool_result_pruning_enabled:
            ctx.model_messages = degrade_tool_results(
                ctx.model_messages,
                keep_recent_n=self._config.tool_result_keep_recent_n,
                max_old_result_chars=self._config.tool_result_max_old_chars,
            )

        # 4. Hard limit — snip history if still over budget
        if self._config.enable_hard_limit:
            estimated = estimate_messages_tokens(ctx.model_messages)
            if estimated > self._config.runtime_hard_limit_tokens:
                ctx.model_messages = snip_history(
                    ctx.model_messages,
                    self._config.runtime_hard_limit_tokens,
                )

    # ══════════════════════════════════════════════════════════════════
    # Context overflow handler (retry / anti-thrashing)
    # ══════════════════════════════════════════════════════════════════

    async def _handle_context_overflow(self, ctx: TurnContext) -> bool:
        """Handle ContextWindowExceededError by degrading context.

        Applies increasingly aggressive degradation steps:

        1. Drop orphaned tool pairs + backfill (already done in governance,
           so skip to next level).
        2. Degrade ALL tool results (not just old ones).
        3. Enable hard limit snip at 50% of normal.
        4. Anti-thrashing: if consecutive ineffective compressions
           exceed threshold, skip future retries.

        Returns:
            True if context was degraded and the loop should retry.
            False if no further degradation is possible.
        """
        # ── Anti-thrashing check ──────────────────────────────────────
        if ctx.ineffective_compression_count >= self._config.anti_thrashing_max_ineffective:
            logger.warning(
                "Anti-thrashing: skipping compression after %d ineffective attempts",
                ctx.ineffective_compression_count,
            )
            return False

        # Record the current token estimate before degrading
        before = estimate_messages_tokens(ctx.model_messages)
        plan_name = ctx.compression_attempts[-1] if ctx.compression_attempts else "none"

        # Determine which degradation step to apply
        attempt = len(ctx.compression_attempts)
        degraded = False

        if attempt == 0:
            # Step 1: Drop orphan tool pairs + backfill (first retry)
            ctx.model_messages = drop_orphan_tool_pairs(ctx.model_messages)
            ctx.model_messages = backfill_missing_tool_results(ctx.model_messages)
            plan_name = "repair_pairs"
            degraded = True
        elif attempt == 1:
            # Step 2: Degrade ALL tool results aggressively
            ctx.model_messages = degrade_tool_results(
                ctx.model_messages,
                keep_recent_n=1,
                max_old_result_chars=200,
            )
            plan_name = "degrade_all_tools"
            degraded = True
        elif attempt == 2:
            # Step 3: Hard snip at 50% of normal hard limit
            half_limit = max(1, self._config.runtime_hard_limit_tokens // 2)
            ctx.model_messages = snip_history(ctx.model_messages, half_limit)
            plan_name = "snip_50pct"
            degraded = True
        elif attempt == 3:
            # Step 4: Hard snip at 10% — keep only system + current user
            ctx.model_messages = snip_history(ctx.model_messages, 1_000)
            plan_name = "snip_minimal"
            degraded = True
        elif attempt == 4 and self._config.enable_iterative_summarization and self._summarizer is not None:
            # Step 5: LLM iterative summarization — replace old history with a summary
            try:
                ctx.model_messages = await self._summarize_old_history(ctx)
                plan_name = "iterative_summary"
                degraded = True
            except Exception as exc:
                logger.warning("Iterative summarization attempt failed: %s", exc)
                # fall through to the else clause
                pass
        else:
            logger.warning("No more degradation plans available after %d attempts", attempt)
            return False

        ctx.compression_attempts.append(plan_name)

        # ── Check effectiveness for anti-thrashing ────────────────────
        after = estimate_messages_tokens(ctx.model_messages)
        if before > 0:
            ratio = (before - after) / before
            logger.debug(
                "Compression plan=%s saved %d tokens (%.1f%%)",
                plan_name,
                before - after,
                ratio * 100,
            )
            if ratio < self._config.anti_thrashing_threshold:
                ctx.ineffective_compression_count += 1
                logger.debug("Ineffective compression count -> %d", ctx.ineffective_compression_count)
            else:
                ctx.ineffective_compression_count = 0
        else:
            ctx.ineffective_compression_count += 1

        return True

    async def _summarize_old_history(self, ctx: TurnContext) -> list[ModelMessage]:
        """Replace old history messages with an LLM-generated summary.

        Preserves the first SystemMessage and the last UserMessage.
        Everything in between is concatenated and sent to the summarizer.
        The result replaces those middle messages.
        """
        summarizer = self._summarizer
        if summarizer is None:
            return ctx.model_messages

        msgs = list(ctx.model_messages)
        if len(msgs) <= 2:
            return msgs  # nothing to summarize

        # Identify the first message (SystemMessage) and last message (UserMessage)
        first_system_idx = 0
        last_user_idx = len(msgs) - 1

        if not isinstance(msgs[last_user_idx], UserMessage):
            return msgs  # safety: last must be UserMessage

        # Extract middle messages for summarization
        middle = msgs[first_system_idx + 1 : last_user_idx]
        if not middle:
            return msgs

        # Build text block from middle messages
        text_parts: list[str] = []
        for m in middle:
            role = type(m).__name__.replace("Message", "").lower()
            content = getattr(m, "content", "") or ""
            if content.strip():
                text_parts.append(f"[{role}]: {content}")

        text = "\n\n".join(text_parts)
        if len(text) > self._config.summarization_max_history_chars:
            text = text[: self._config.summarization_max_history_chars]

        # Get existing summary from context
        existing = ctx.session_summary.content if ctx.session_summary else None

        # Call summarizer
        summary = await summarizer.summarize(
            text=text,
            existing_summary=existing,
            max_output_tokens=512,
            timeout_seconds=self._config.summarization_timeout_seconds,
        )

        if not summary.strip():
            return msgs  # summarization produced nothing useful

        # Replace middle messages with a single summary message
        summary_msg = UserMessage(
            content=f"[对话历史摘要]\n{summary}",
            metadata={"kind": "history_summary", "compressed": True},
        )

        return [msgs[first_system_idx], summary_msg, msgs[last_user_idx]]

    # ══════════════════════════════════════════════════════════════════
    # Model invocation
    # ══════════════════════════════════════════════════════════════════

    async def _invoke_model(
        self,
        ctx: TurnContext,
        request: ModelInvocationRequest,
    ) -> ModelRoundOutput:
        assembler = self._assembler_factory(
            max_text_chars=self._config.max_model_text_chars,
        )
        started_at = self._clock.now()

        await self._emit_safe(
            ctx,
            AgentEventType.MODEL_CALL_STARTED,
            {
                "round_index": request.round_index,
                "message_count": len(request.messages),
                "tool_count": len(request.tools),
            },
        )

        try:
            async with asyncio.timeout(request.timeout_seconds):
                async with aclosing(self._model.stream(request)) as stream:
                    async for event in stream:
                        self._check_cancellation_and_deadline(ctx)
                        assembler.accept(event)

                        if assembler.is_public_text_delta(event):
                            await self._emit_safe(
                                ctx,
                                AgentEventType.MODEL_DELTA,
                                assembler.public_delta_payload(event),
                            )

        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ModelInvocationTimeoutError(
                "Model invocation timed out",
                safe_message="模型响应超时",
            ) from exc
        except Exception as exc:
            raise ModelInvocationError(
                f"Model invocation failed: {exc}",
                safe_message="模型调用失败",
            ) from exc

        output = assembler.build(round_index=request.round_index)
        duration_ms = self._duration_ms(started_at, self._clock.now())

        await self._emit_safe(
            ctx,
            AgentEventType.MODEL_CALL_COMPLETED,
            {
                "round_index": output.round_index,
                "finish_reason": output.finish_reason,
                "output_mode": "final_response" if output.text is not None else "tool_calls",
                "input_tokens": output.input_tokens,
                "output_tokens": output.output_tokens,
                "tool_call_count": len(output.tool_calls),
                "duration_ms": duration_ms,
            },
        )

        return output

    # ══════════════════════════════════════════════════════════════════
    # Tool call preparation
    # ══════════════════════════════════════════════════════════════════

    def _check_tool_budget(
        self,
        ctx: TurnContext,
        tool_calls: tuple[ToolCall, ...],
    ) -> None:
        """Check budget limits before preparing tool calls."""
        if len(tool_calls) > self._config.max_tool_calls_per_round:
            raise MaxToolCallsPerRoundExceededError(
                f"Round tool calls {len(tool_calls)} > {self._config.max_tool_calls_per_round}",
                safe_message="单轮工具调用数量超限",
            )

        projected = ctx.total_tool_calls_used + len(tool_calls)
        if projected > self._config.max_total_tool_calls:
            raise MaxTotalToolCallsExceededError(
                f"Total tool calls {projected} > {self._config.max_total_tool_calls}",
                safe_message="工具调用总数超限",
            )

        projected_rounds = ctx.tool_rounds_used + 1
        if projected_rounds > self._config.max_tool_rounds:
            raise MaxTotalToolCallsExceededError(
                # This error type is defined but unused for rounds; reuse for clarity
                f"Tool rounds {projected_rounds} > {self._config.max_tool_rounds}",
                safe_message="工具调用轮数超限",
            )

    def _prepare_tool_plan(
        self,
        ctx: TurnContext,
        tool_calls: tuple[ToolCall, ...],
    ) -> ToolCallPlan:
        """Resolve names, validate arguments, fingerprint each call."""
        seen_ids: set[str] = set()
        executable: list[PreparedToolCall] = []
        rejected: list[RejectedToolCall] = []

        for tc in tool_calls:
            # Duplicate call_id check
            if tc.call_id in seen_ids:
                raise DuplicateToolCallIdError(
                    f"Duplicate tool call_id: {tc.call_id}",
                    safe_message="工具调用标识重复",
                )
            seen_ids.add(tc.call_id)

            # Resolve name
            definition = self._tool_registry.resolve_from_list(
                name=tc.tool_name,
                available_tools=tuple(ctx.available_tools),
            )
            if definition is None:
                rejected.append(
                    RejectedToolCall(
                        call=tc,
                        arguments_fingerprint=self._fingerprint(tc.tool_name, tc.arguments),
                        error_code="UNKNOWN_TOOL",
                        safe_message=f"未知工具: {tc.tool_name}",
                    ),
                )
                continue

            # Validate arguments
            try:
                self._tool_registry.validate_arguments(
                    definition=definition,
                    arguments=tc.arguments,
                )
            except Exception as exc:
                rejected.append(
                    RejectedToolCall(
                        call=tc,
                        arguments_fingerprint=self._fingerprint(tc.tool_name, tc.arguments),
                        error_code="INVALID_ARGUMENTS",
                        safe_message=str(exc),
                    ),
                )
                continue

            # Build prepared call
            fp = self._fingerprint(tc.tool_name, tc.arguments)
            key = f"{ctx.turn_id}:{tc.call_id}"
            executable.append(
                PreparedToolCall(
                    call=tc,
                    definition=definition,
                    idempotency_key=key,
                    arguments_fingerprint=fp,
                ),
            )

        return ToolCallPlan(
            original_calls=tool_calls,
            executable_calls=tuple(executable),
            rejected_calls=tuple(rejected),
        )

    @staticmethod
    def _fingerprint(tool_name: str, arguments: Mapping[str, object]) -> str:
        """Compute SHA-256 fingerprint: tool_name + canonical JSON."""
        canonical = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        source = f"{tool_name}\n{canonical}"
        return sha256(source.encode("utf-8")).hexdigest()

    # ══════════════════════════════════════════════════════════════════
    # Policy evaluation
    # ══════════════════════════════════════════════════════════════════

    async def _evaluate_policy(
        self,
        ctx: TurnContext,
        executable_calls: tuple[PreparedToolCall, ...],
    ) -> list[tuple[PreparedToolCall, ToolPolicyDecision]]:
        results: list[tuple[PreparedToolCall, ToolPolicyDecision]] = []
        for prepared in executable_calls:
            decision = await self._tool_policy.evaluate(
                actor_id=ctx.request.actor_id,
                session_id=ctx.request.session_id,
                prepared_call=prepared,
            )
            results.append((prepared, decision))
        return results

    @staticmethod
    def _requires_approval(
        decisions: list[tuple[PreparedToolCall, ToolPolicyDecision]],
    ) -> bool:
        return any(
            d.decision is ToolPolicyDecisionType.REQUIRE_APPROVAL
            for _, d in decisions
        )

    async def _suspend_for_approval(
        self,
        ctx: TurnContext,
        plan: ToolCallPlan,
        decisions: list[tuple[PreparedToolCall, ToolPolicyDecision]],
    ) -> None:
        """Build PendingApprovalBatch and checkpoint, set WAITING_APPROVAL."""
        self._check_cancellation_and_deadline(ctx)

        items: list[PendingApprovalItem] = []
        for prepared, decision in decisions:
            if decision.decision is ToolPolicyDecisionType.REQUIRE_APPROVAL:
                items.append(
                    PendingApprovalItem(
                        call=prepared.call,
                        tool_name=prepared.call.tool_name,
                        risk_level=prepared.definition.risk_level,
                        side_effect=prepared.definition.side_effect,
                        reason_code=decision.reason_code,
                        approval_prompt=decision.approval_prompt or "",
                    ),
                )

        now = self._clock.now()
        approval_id = f"approval_{ctx.turn_id}_{now.strftime('%Y%m%d_%H%M%S')}"

        pending = PendingApprovalBatch(
            approval_id=approval_id,
            turn_id=ctx.turn_id or "",
            actor_id=ctx.request.actor_id,
            session_id=ctx.request.session_id,
            created_at=now,
            expires_at=None,
            items=tuple(items),
        )

        # Build serialisable checkpoint
        integrity_parts = [
            ctx.request.actor_id,
            ctx.request.session_id,
            approval_id,
        ]
        for prepared, _ in decisions:
            integrity_parts.append(prepared.call.call_id)
            integrity_parts.append(prepared.arguments_fingerprint)

        checkpoint = AgentLoopCheckpoint(
            original_turn_id=ctx.turn_id or "",
            approval_id=approval_id,
            model_messages=tuple(ctx.model_messages),
            tool_plan=plan,
            model_calls_used=ctx.model_calls_used,
            tool_rounds_used=ctx.tool_rounds_used,
            total_tool_calls_used=ctx.total_tool_calls_used,
            usage=ctx.usage,
            integrity_hash=sha256(
                "\n".join(integrity_parts).encode("utf-8"),
            ).hexdigest(),
        )

        ctx.pending_approval = pending
        ctx.loop_checkpoint = checkpoint
        ctx.output_text = self._config.approval_message

        # Also set status so Kernel/finalize know
        ctx.status = TurnStatus.WAITING_APPROVAL

        await self._emit_safe(
            ctx,
            AgentEventType.TOOL_APPROVAL_REQUIRED,
            {
                "approval_id": approval_id,
                "tool_count": len(items),
                "items": [
                    {
                        "call_id": item.call.call_id,
                        "tool_name": item.tool_name,
                        "risk_level": item.risk_level,
                        "approval_prompt": item.approval_prompt,
                    }
                    for item in items
                ],
            },
        )

        await self._emit_safe(
            ctx,
            AgentEventType.TURN_SUSPENDED,
            {"reason": "approval_required", "approval_id": approval_id},
        )

    # ══════════════════════════════════════════════════════════════════
    # Checkpoint resume
    # ══════════════════════════════════════════════════════════════════

    async def _resume_from_checkpoint(
        self,
        ctx: TurnContext,
        loop_guard: ToolLoopGuard,
    ) -> None:
        """Resume execution after an approval decision."""
        checkpoint = ctx.loop_checkpoint
        if checkpoint is None:
            return

        # Restore canonical messages from checkpoint
        ctx.model_messages = list(checkpoint.model_messages)
        ctx.model_calls_used = checkpoint.model_calls_used
        ctx.tool_rounds_used = checkpoint.tool_rounds_used
        ctx.total_tool_calls_used = checkpoint.total_tool_calls_used
        ctx.usage = checkpoint.usage

        decision = ctx.request.control
        if decision is None:
            # No decision yet — stay suspended (shouldn't reach here)
            return

        if decision.approval_id != checkpoint.approval_id:
            raise InvalidApprovalCheckpointError(
                f"Approval ID mismatch: {decision.approval_id} vs {checkpoint.approval_id}",
                safe_message="审批标识不匹配",
            )

        # Replay the checkpoint's tool plan with decisions
        plan = checkpoint.tool_plan

        results: list[ToolExecutionResult] = []
        for tc in plan.original_calls:
            action = decision.actions.get(tc.call_id, ApprovalAction.REJECT)

            if action is ApprovalAction.APPROVE:
                prepared = next(
                    (p for p in plan.executable_calls if p.call.call_id == tc.call_id),
                    None,
                )
                if prepared is not None:
                    exec_ctx = ToolExecutionContext(
                        turn_id=ctx.turn_id or "",
                        request_id=ctx.request.request_id,
                        session_id=ctx.request.session_id,
                        actor_id=ctx.request.actor_id,
                        call_id=tc.call_id,
                        idempotency_key=prepared.idempotency_key,
                        deadline_at=ctx.deadline_at,
                    )
                    try:
                        result = await self._execute_one_tool(ctx, prepared, exec_ctx)
                        results.append(result)
                    except Exception:
                        results.append(
                            ToolExecutionResult(
                                call_id=tc.call_id,
                                tool_name=tc.tool_name,
                                status=ToolExecutionStatus.FAILED,
                                model_content='{"error":{"code":"RESUME_FAILURE"}}',
                                safe_message="恢复执行失败",
                                error_code="RESUME_FAILURE",
                            ),
                        )
                else:
                    results.append(
                        ToolExecutionResult(
                            call_id=tc.call_id,
                            tool_name=tc.tool_name,
                            status=ToolExecutionStatus.FAILED,
                            model_content='{"error":{"code":"NOT_FOUND"}}',
                            safe_message="工具调用未找到",
                            error_code="NOT_FOUND",
                        ),
                    )
            else:
                # Rejected
                results.append(
                    ToolExecutionResult(
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        status=ToolExecutionStatus.DENIED,
                        model_content=json.dumps(
                            {
                                "error": {
                                    "code": "DENIED_BY_USER",
                                    "message": "用户拒绝了该操作",
                                },
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        safe_message="用户拒绝了该操作",
                        error_code="DENIED_BY_USER",
                    ),
                )

        # Append tool results in ordinal order
        self._append_tool_results(ctx, plan.original_calls, results)

        # Record in loop guard
        loop_guard.record_batch(plan, results)

        # Update counters
        ctx.tool_rounds_used += 1
        ctx.total_tool_calls_used += len(plan.original_calls)

        # Clear checkpoint (it has been consumed) and restore RUNNING status
        ctx.loop_checkpoint = None
        ctx.pending_approval = None
        ctx.output_text = None
        ctx.status = TurnStatus.RUNNING

    # ══════════════════════════════════════════════════════════════════
    # Tool execution
    # ══════════════════════════════════════════════════════════════════

    async def _execute_tool_plan(
        self,
        ctx: TurnContext,
        plan: ToolCallPlan,
        decisions: list[tuple[PreparedToolCall, ToolPolicyDecision]],
    ) -> list[ToolExecutionResult]:
        """Execute a batch of tools with policy decisions applied."""
        results: list[ToolExecutionResult] = []

        # Map call_id → decision
        decision_map: dict[str, ToolPolicyDecisionType] = {
            p.call.call_id: d.decision for p, d in decisions
        }

        # Split into parallel and sequential groups
        parallel_group: list[PreparedToolCall] = []
        for prepared in plan.executable_calls:
            decision_type = decision_map.get(prepared.call.call_id)
            if decision_type is ToolPolicyDecisionType.ALLOW:
                if (
                    prepared.definition.parallel_safe
                    and prepared.definition.side_effect == ToolSideEffect.NONE
                ):
                    parallel_group.append(prepared)
                else:
                    # Execute one at a time
                    result = await self._execute_one_with_timeout(ctx, prepared)
                    results.append(result)
            elif decision_type is ToolPolicyDecisionType.DENY:
                # Find the decision's safe message
                safe_msg = ""
                for p, d in decisions:
                    if p.call.call_id == prepared.call.call_id:
                        safe_msg = d.safe_message
                        break
                results.append(self._deny_result(prepared, safe_msg))

        # Execute parallel group
        if parallel_group:
            semaphore = asyncio.Semaphore(self._config.max_parallel_tools)

            async def run_one(prepared: PreparedToolCall) -> ToolExecutionResult:
                async with semaphore:
                    return await self._execute_one_with_timeout(ctx, prepared)

            async with asyncio.TaskGroup() as tg:
                tasks = {
                    tg.create_task(run_one(prepared)): prepared
                    for prepared in parallel_group
                }

            for task, prepared in tasks.items():
                try:
                    results.append(task.result())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    results.append(
                        ToolExecutionResult(
                            call_id=prepared.call.call_id,
                            tool_name=prepared.call.tool_name,
                            status=ToolExecutionStatus.FAILED,
                            model_content='{"error":{"code":"EXECUTION_FAILURE"}}',
                            safe_message="工具执行失败",
                            error_code="EXECUTION_FAILURE",
                            retryable=False,
                        ),
                    )

        # Add rejected call results
        for rejected in plan.rejected_calls:
            results.append(
                ToolExecutionResult(
                    call_id=rejected.call.call_id,
                    tool_name=rejected.call.tool_name,
                    status=ToolExecutionStatus.FAILED,
                    model_content=json.dumps(
                        {
                            "error": {
                                "code": rejected.error_code,
                                "message": rejected.safe_message,
                            },
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    safe_message=rejected.safe_message,
                    error_code=rejected.error_code,
                    retryable=False,
                ),
            )

        # Sort by ordinal to preserve model-declared order
        results.sort(key=lambda r: _ordinal_for_call(r.call_id, plan.original_calls))
        return results

    async def _execute_one_with_timeout(
        self,
        ctx: TurnContext,
        prepared: PreparedToolCall,
    ) -> ToolExecutionResult:
        timeout = min(
            prepared.definition.timeout_seconds,
            self._remaining_call_timeout(ctx),
        )
        if timeout <= 0:
            raise TurnDeadlineExceededError(
                "No time remaining for tool execution",
                safe_message="响应超时",
            )

        exec_ctx = ToolExecutionContext(
            turn_id=ctx.turn_id or "",
            request_id=ctx.request.request_id,
            session_id=ctx.request.session_id,
            actor_id=ctx.request.actor_id,
            call_id=prepared.call.call_id,
            idempotency_key=prepared.idempotency_key,
            deadline_at=ctx.deadline_at,
        )

        try:
            async with asyncio.timeout(timeout):
                return await self._execute_one_tool(ctx, prepared, exec_ctx)
        except TimeoutError:
            return ToolExecutionResult(
                call_id=prepared.call.call_id,
                tool_name=prepared.call.tool_name,
                status=ToolExecutionStatus.TIMED_OUT,
                model_content=(
                    '{"error":{"code":"TOOL_TIMEOUT",'
                    '"message":"工具执行超时"}}'
                ),
                safe_message="工具执行超时",
                error_code="TOOL_TIMEOUT",
                retryable=True,
            )

    async def _execute_one_tool(
        self,
        ctx: TurnContext,
        prepared: PreparedToolCall,
        exec_ctx: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = self._clock.now()

        await self._emit_safe(
            ctx,
            AgentEventType.TOOL_CALL_STARTED,
            {
                "round_index": ctx.model_calls_used,
                "call_id": prepared.call.call_id,
                "tool_name": prepared.call.tool_name,
                "ordinal": prepared.call.ordinal,
                "arguments_fingerprint": prepared.arguments_fingerprint[:12],
            },
        )

        try:
            result = await self._tool_executor.execute(
                prepared_call=prepared,
                context=exec_ctx,
            )
        except Exception as exc:
            duration_ms = self._duration_ms(started_at, self._clock.now())
            ctx.tool_records.append(
                ToolExecutionRecord(
                    call_id=prepared.call.call_id,
                    tool_name=prepared.call.tool_name,
                    status=ToolExecutionStatus.FAILED,
                    started_at=started_at,
                    completed_at=self._clock.now(),
                    duration_ms=duration_ms,
                    error_code=exc.__class__.__name__,
                    retryable=False,
                    idempotency_key=prepared.idempotency_key,
                    arguments_fingerprint=prepared.arguments_fingerprint,
                ),
            )

            await self._emit_safe(
                ctx,
                AgentEventType.TOOL_CALL_FAILED,
                {
                    "call_id": prepared.call.call_id,
                    "tool_name": prepared.call.tool_name,
                    "status": "failed",
                    "error_code": exc.__class__.__name__,
                    "retryable": False,
                    "duration_ms": duration_ms,
                },
            )

            return ToolExecutionResult(
                call_id=prepared.call.call_id,
                tool_name=prepared.call.tool_name,
                status=ToolExecutionStatus.FAILED,
                model_content='{"error":{"code":"EXECUTOR_ERROR"}}',
                safe_message="工具执行器异常",
                error_code="EXECUTOR_ERROR",
            )

        # Validate result
        if result.call_id != prepared.call.call_id:
            raise ToolResultProtocolError(
                f"Expected call_id {prepared.call.call_id}, got {result.call_id}",
                safe_message="工具结果标识不匹配",
            )

        # Truncate long results
        max_chars = prepared.definition.max_result_chars
        truncated = result.model_content[:max_chars]
        if len(result.model_content) > max_chars:
            truncated += "\n[截断: 结果过长]"

        duration_ms = self._duration_ms(started_at, self._clock.now())
        ctx.tool_records.append(
            ToolExecutionRecord(
                call_id=prepared.call.call_id,
                tool_name=prepared.call.tool_name,
                status=result.status,
                started_at=started_at,
                completed_at=self._clock.now(),
                duration_ms=duration_ms,
                error_code=result.error_code,
                retryable=result.retryable,
                idempotency_key=prepared.idempotency_key,
                arguments_fingerprint=prepared.arguments_fingerprint,
            ),
        )

        await self._emit_safe(
            ctx,
            AgentEventType.TOOL_CALL_COMPLETED,
            {
                "call_id": prepared.call.call_id,
                "tool_name": prepared.call.tool_name,
                "status": result.status,
                "duration_ms": duration_ms,
                "artifact_count": len(result.artifacts),
            },
        )

        return ToolExecutionResult(
            call_id=result.call_id,
            tool_name=result.tool_name,
            status=result.status,
            model_content=truncated,
            safe_message=result.safe_message,
            error_code=result.error_code,
            retryable=result.retryable,
            artifacts=result.artifacts,
            metadata=result.metadata,
        )

    # ══════════════════════════════════════════════════════════════════
    # Result appending
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _append_tool_results(
        ctx: TurnContext,
        original_calls: tuple[ToolCall, ...],
        results: list[ToolExecutionResult],
    ) -> None:
        """Append ToolMessages in the original ordinal order."""
        result_map = {r.call_id: r for r in results}

        for tc in original_calls:
            result = result_map.get(tc.call_id)
            if result is None:
                content = '{"error":{"code":"MISSING_RESULT"}}'
                is_error = True
            else:
                content = result.model_content
                is_error = result.status is not ToolExecutionStatus.SUCCEEDED

            ctx.model_messages.append(
                ToolMessage(
                    tool_call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    content=content,
                    is_error=is_error,
                ),
            )

    @staticmethod
    def _deny_result(
        prepared: PreparedToolCall,
        safe_message: str,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=prepared.call.call_id,
            tool_name=prepared.call.tool_name,
            status=ToolExecutionStatus.DENIED,
            model_content=json.dumps(
                {
                    "error": {
                        "code": "POLICY_DENIED",
                        "message": safe_message or "策略拒绝",
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            safe_message=safe_message or "策略拒绝",
            error_code="POLICY_DENIED",
            retryable=False,
        )

    # ══════════════════════════════════════════════════════════════════
    # Time helpers
    # ══════════════════════════════════════════════════════════════════

    def _remaining_call_timeout(self, ctx: TurnContext) -> float:
        """Compute min(configured_timeout, remaining_turn_time)."""
        if ctx.deadline_at is None:
            return self._config.model_call_timeout_seconds

        remaining = (ctx.deadline_at - self._clock.now()).total_seconds()
        if remaining <= 0:
            raise TurnDeadlineExceededError(
                "No time remaining for model call",
                safe_message="响应超时",
            )
        return min(self._config.model_call_timeout_seconds, remaining)

    @staticmethod
    def _duration_ms(start: datetime, end: datetime) -> int:
        return int((end - start).total_seconds() * 1000)

    # ══════════════════════════════════════════════════════════════════
    # Event emission
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def _emit_safe(
        ctx: TurnContext,
        event_type: AgentEventType,
        data: Mapping[str, object] | None = None,
    ) -> None:
        emitter = ctx.event_emitter
        if emitter is not None:
            await emitter.emit(
                event_type,
                phase="agent_loop",
                data=data,
            )


def _ordinal_for_call(call_id: str, original_calls: tuple[ToolCall, ...]) -> int:
    for tc in original_calls:
        if tc.call_id == call_id:
            return tc.ordinal
    return 9999
