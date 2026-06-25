# cogito/agent/tools/orchestrator.py
#
# DefaultToolOrchestrator — full 19-step tool execution pipeline.
#
# Design rules (see tool-system-spec §11):
#   - Fixed execution order, not swappable.
#   - Approval always precedes execution.
#   - Secret redaction must happen before logging/model injection.
#   - Result persistence must happen before context trimming.
#
# Execution pipeline (tool-system-spec §11.1):
#   1. Resolve          6. Context Enrichment   11. Timeout/Cancel    16. Persistence
#   2. Visibility Check 7. Policy Evaluation    12. Handler Execute   17. Audit Record
#   3. Argument Parse   8. Approval Resolution  13. Output Validate   18. Event Emission
#   4. Type Coercion    9. Rate Limit           14. Secret Redaction  19. Return ToolResult
#   5. Schema Validate  10. Concurrency Lock    15. Result Normalize

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Mapping

from cogito.agent.domain.tools import (
    PreparedToolCall,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolResult,
    ToolResultStatus,
)
from cogito.agent.ports.tools.executor import ToolExecutionContext
from cogito.agent.ports.tools.registry import ToolHandler, ToolRegistrySnapshot
from cogito.agent.tools.concurrency import ToolConcurrencyController
from cogito.agent.tools.context_governor import ContextGovernor
from cogito.agent.tools.hooks import HookExecutor
from cogito.agent.tools.repetition_guard import RepetitionGuard
from cogito.infrastructure.tools.rate_limiter import TokenBucketRateLimiter
from cogito.agent.tools.result_processor import DefaultToolResultProcessor
from cogito.agent.tools.validation import JsonSchemaToolValidator

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    default_tool_timeout_seconds: float = 60.0
    max_parallel_calls: int = 4
    argument_max_bytes: int = 262_144


class DefaultToolOrchestrator:
    """Complete tool execution pipeline with full governance."""

    def __init__(
        self,
        *,
        registry: ToolRegistrySnapshot,
        validator: JsonSchemaToolValidator,
        result_processor: DefaultToolResultProcessor,
        concurrency: ToolConcurrencyController,
        config: OrchestratorConfig | None = None,
        hook_executor: HookExecutor | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self._registry = registry
        self._validator = validator
        self._result_processor = result_processor
        self._concurrency = concurrency
        self._config = config or OrchestratorConfig()
        self._hook_executor = hook_executor
        self._rate_limiter = rate_limiter

    # ══════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════

    async def execute(
        self,
        *,
        call: ToolCall,
        definition: ToolDefinition,
        context: ToolExecutionContext,
        guard: RepetitionGuard | None = None,
    ) -> ToolResult:
        """Execute a single tool call through the full pipeline.

        Steps 1-2 (Resolve + Visibility) are expected to be done
        by the caller (AgentLoopPhase). This method runs steps 3-19.
        """
        started = datetime.now()

        try:
            # Step 3: Argument Parse — parse JSON if needed
            raw_args = self._parse_arguments(call)

            # Step 4: Type Coercion
            args = self._coerce_arguments(raw_args, definition)

            # Step 5: Schema Validation
            self._validator.validate(definition=definition, arguments=args)

            # Step 6: Context Enrichment
            exec_ctx = self._enrich_context(context, args)

            # Step 6.5: Pre-Tool Hooks (can deny or modify arguments)
            if self._hook_executor is not None:
                deny_outcome, pre_trace, hook_args = await self._hook_executor.run_pre_hooks(
                    tool_name=call.tool_name,
                    arguments=dict(args),
                    definition=definition_to_dict(definition),
                    context=exec_ctx,
                )
                if deny_outcome is not None:
                    return ToolResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        status=ToolResultStatus.ERROR,
                        status_code="HOOK_DENIED",
                        llm_content=deny_outcome.reason or "工具调用被规则拒绝",
                        display_content=deny_outcome.reason or "Tool call denied by policy hook",
                        timestamp=datetime.now(),
                    )
                # Use potentially modified arguments
                args = hook_args

            # Step 7: Policy Evaluation — handled by AgentLoopPhase

            # Step 8: Approval Resolution — handled by AgentLoopPhase

            # Step 9: Rate Limit
            if self._rate_limiter is not None:
                allowed = await self._rate_limiter.acquire(
                    call.tool_name,
                    session_id=context.session_id,
                    timeout=5.0,
                )
                if not allowed:
                    return self._result_processor.build_error_result(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        error_code="RATE_LIMITED",
                        safe_message="工具调用频率过高，请稍后再试",
                        retryable=True,
                    )

            # Step 10: Concurrency Lock
            token = await self._concurrency.acquire(
                definition=definition,
                session_id=context.session_id,
            )
            try:
                # Step 11: Timeout / Cancellation Scope
                timeout = definition.limits.timeout_seconds or self._config.default_tool_timeout_seconds

                # Step 12: Handler Execute
                handler = self._resolve_handler(definition.name)
                try:
                    async with asyncio.timeout(timeout):
                        raw_result = await handler.execute(
                            arguments=args,
                            context=exec_ctx,
                        )
                except TimeoutError:
                    elapsed = int((datetime.now() - started).total_seconds() * 1000)
                    return self._result_processor.build_error_result(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        error_code="TOOL_TIMEOUT",
                        safe_message="工具执行超时",
                        retryable=True,
                    )
            finally:
                await token.release()

            # Step 13: Output Schema Validation (optional)
            if definition.output_schema:
                output_errors = self._validator.validate_output(
                    definition=definition,
                    output=raw_result if isinstance(raw_result, dict) else {},
                )

            # Step 14: Secret Redaction — handled by result_processor
            # Step 15: Result Normalization
            # Step 16: Persistence
            result = await self._result_processor.process(
                definition=definition,
                result=raw_result,
                context={"call_id": call.call_id},
            )

            # Step 17: Audit Record — handled by caller
            # Step 18: Event Emission — handled by caller

            # Step 18.5: Post-Tool Hooks
            if self._hook_executor is not None:
                post_trace, extra_msgs = await self._hook_executor.run_post_hooks(
                    tool_name=call.tool_name,
                    arguments=args,
                    raw_result=raw_result,
                    definition=definition_to_dict(definition),
                    context=exec_ctx,
                )
                if extra_msgs:
                    # Append extra messages to result
                    existing = result.llm_content or ""
                    extra = "\n".join(extra_msgs)
                    from dataclasses import replace
                    result = replace(
                        result,
                        llm_content=(existing + "\n" + extra) if existing else extra,
                    )

            # Step 19: Return ToolResult
            return result

        except asyncio.CancelledError:
            return self._result_processor.build_error_result(
                call_id=call.call_id,
                tool_name=call.tool_name,
                error_code="TOOL_CANCELLED",
                safe_message="工具执行已取消",
            )
        except Exception as exc:
            # Error hooks
            if self._hook_executor is not None:
                await self._hook_executor.run_error_hooks(
                    tool_name=call.tool_name,
                    arguments=dict(args) if 'args' in dir() else {},
                    error=str(exc),
                    definition=definition_to_dict(definition) if 'definition' in dir() else {},
                )
            logger.exception("Tool execution failed: %s", call.tool_name)
            return self._result_processor.build_error_result(
                call_id=call.call_id,
                tool_name=call.tool_name,
                error_code=exc.__class__.__name__,
                safe_message=str(exc)[:200] or "工具执行失败",
                retryable=getattr(exc, "retryable", False),
            )

    async def execute_many(
        self,
        *,
        calls: tuple[ToolCall, ...],
        definitions: dict[str, ToolDefinition],
        context: ToolExecutionContext,
        guard: RepetitionGuard | None = None,
    ) -> tuple[ToolResult, ...]:
        """Execute multiple tool calls, parallelizing when safe."""
        results: list[ToolResult] = []

        # Group by concurrency mode
        parallel_group: list[ToolCall] = []
        for tc in calls:
            defn = definitions.get(tc.tool_name)
            if defn is not None and self._concurrency.can_parallel([defn]):
                parallel_group.append(tc)
            else:
                # Execute serially
                result = await self.execute(
                    call=tc,
                    definition=defn or self._build_fallback_definition(tc),
                    context=context,
                    guard=guard,
                )
                results.append(result)

        # Execute parallel group
        if parallel_group:
            async def run_one(tc: ToolCall) -> ToolResult:
                defn = definitions.get(tc.tool_name) or self._build_fallback_definition(tc)
                return await self.execute(call=tc, definition=defn, context=context, guard=guard)

            async with asyncio.TaskGroup() as tg:
                tasks = {tg.create_task(run_one(tc)): tc for tc in parallel_group}

            for task in tasks:
                try:
                    results.append(task.result())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tc = tasks[task]
                    results.append(
                        self._result_processor.build_error_result(
                            call_id=tc.call_id,
                            tool_name=tc.tool_name,
                            error_code="TASK_GROUP_ERROR",
                            safe_message=str(exc)[:200],
                        ),
                    )

        # Restore ordinal order
        call_order = {tc.call_id: i for i, tc in enumerate(calls)}
        results.sort(key=lambda r: call_order.get(r.call_id, 9999))

        return tuple(results)

    # ══════════════════════════════════════════════════════════════════
    # Internal helpers
    # ══════════════════════════════════════════════════════════════════

    def _parse_arguments(self, call: ToolCall) -> Mapping[str, object]:
        """Parse and validate argument size."""
        args_json = call.arguments_json.encode("utf-8")
        if len(args_json) > self._config.argument_max_bytes:
            raise ValueError(
                f"Tool call arguments exceed {self._config.argument_max_bytes} bytes",
            )
        return call.arguments

    @staticmethod
    def _coerce_arguments(
        args: Mapping[str, object],
        definition: ToolDefinition,
    ) -> Mapping[str, object]:
        """Apply type coercion to arguments."""
        # Simplified: only handles string→number coercion for known fields
        coerced = dict(args)
        schema = definition.input_schema
        properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}

        for key, value in list(coerced.items()):
            prop_schema = properties.get(key, {})
            if not isinstance(prop_schema, dict):
                continue

            expected_type = prop_schema.get("type")

            # String → Integer
            if expected_type == "integer" and isinstance(value, str):
                try:
                    coerced[key] = int(value)
                except (ValueError, TypeError):
                    pass

            # String → Number
            elif expected_type == "number" and isinstance(value, str):
                try:
                    coerced[key] = float(value)
                except (ValueError, TypeError):
                    pass

            # String → Boolean
            elif expected_type == "boolean" and isinstance(value, str):
                if value.lower() in ("true", "1"):
                    coerced[key] = True
                elif value.lower() in ("false", "0"):
                    coerced[key] = False

        return coerced

    @staticmethod
    def _enrich_context(
        context: ToolExecutionContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        """Enrich the execution context with turn and argument data."""
        return {
            "turn_id": context.turn_id,
            "request_id": context.request_id,
            "session_id": context.session_id,
            "actor_id": context.actor_id,
            "call_id": context.call_id,
            "arguments": arguments,
        }

    def _resolve_handler(self, name: str) -> ToolHandler:
        """Resolve a tool handler from the registry snapshot."""
        handler = self._registry.handlers.get(name)
        if handler is None:
            raise KeyError(f"Tool handler not found: {name!r}")
        return handler

    @staticmethod
    def _build_fallback_definition(tc: ToolCall) -> ToolDefinition:
        """Build a minimal fallback definition for unknown tools."""
        from cogito.agent.domain.tools import (
            ToolConcurrencyMode,
            ToolKind,
            ToolLimits,
            ToolRisk,
            ToolRiskLevel,
            ToolSideEffect,
            ToolSource,
            ToolSourceType,
        )
        return ToolDefinition(
            name=tc.tool_name,
            description="Fallback definition for unknown tool",
            input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=30.0,
            idempotent=False,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="fallback"),
        )


def definition_to_dict(definition: ToolDefinition) -> dict[str, object]:
    """Convert a ToolDefinition to a serialisable dict for hooks."""
    return {
        "name": definition.name,
        "description": definition.description,
        "risk": definition.risk.value if hasattr(definition.risk, "value") else str(definition.risk),
        "risk_level": definition.risk_level.value if hasattr(definition.risk_level, "value") else str(definition.risk_level),
        "side_effect": definition.side_effect.value if hasattr(definition.side_effect, "value") else str(definition.side_effect),
        "kind": definition.kind.value if hasattr(definition.kind, "value") else str(definition.kind),
        "timeout_seconds": definition.timeout_seconds,
        "idempotent": definition.idempotent,
        "parallel_safe": definition.parallel_safe,
        "enabled": definition.enabled,
    }
