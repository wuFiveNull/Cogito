"""ToolExecutor — 执行工具调用。

TOOL-SANDBOX / 1. 执行链：
ToolRequest → Registry resolve → input schema → Policy → persist → execute → persist → return ToolResult

当前阶段实现：
- Registry resolve
- Policy evaluation（TOOL-SANDBOX / 3）
- 参数校验（JSON Schema / TypeAdapter）
- Handler 调度
- 结果格式化
- ToolCallRepository 持久化（TOOL-SANDBOX / 2）
- 并发执行（asyncio.gather）
- 输出大小限制（TOOL-SANDBOX / 10）
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from cogito.capability.auto_mode import AutoModeGate
from cogito.capability.models import (
    ConstraintSet,
    DeferredExecution,
    ToolCallState,
    ToolContext,
    ToolDef,
    ToolResult,
)
from cogito.capability.policy import ToolPolicy
from cogito.capability.registry import CapabilityRegistry
from cogito.contracts.clock import epoch_ms

# 最大输出字符数
MAX_TOOL_OUTPUT_CHARS = 100_000


class ToolValidationError(Exception):
    """参数校验失败。"""

    pass


class ToolExecutionError(Exception):
    """工具执行失败。"""

    pass


class ToolExecutor:
    """工具执行器。

    职责：
    - 按名称解析 ToolDef
    - 策略评估（allow/deny）
    - 校验参数
    - 执行 handler
    - 持久化 ToolCall 记录
    - 格式化 ToolResult
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy: ToolPolicy | None = None,
        sink: Any | None = None,  # ToolCallSink (PLAN-09 M4a)
        on_event: Callable[[dict[str, Any]], None] | None = None,
        auto_mode: AutoModeGate | None = None,
        approval_service: Any | None = None,
        payload_store: Any | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy or ToolPolicy()
        self._sink = sink  # ToolCallSink — 由组合根注入
        self._on_event = on_event  # Event metadata callback (D8)
        self._auto_mode = auto_mode
        self._approval_service = approval_service
        self._payload_store = payload_store

    async def execute(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """执行单个工具调用。

        执行链：
        1. Registry resolve
        2. Policy evaluation
        3. 持久化 "executing" 状态
        4. 参数校验
        5. Handler 执行
        6. 结果截断
        7. 持久化最终状态
        """
        # 1. Resolve
        tool = self._registry.get(tool_name)
        if tool is None:
            stale = self._registry.is_stale(tool_name)
            return ToolResult(
                tool_call_id,
                tool_name,
                "error",
                error_message=(
                    f"Stale capability '{tool_name}': provider no longer exposes this Tool"
                    if stale
                    else f"Tool '{tool_name}' not found in registry"
                ),
            )
        if context.capability_snapshot_ids and tool.capability_id not in set(
            context.capability_snapshot_ids
        ):
            return ToolResult(
                tool_call_id,
                tool_name,
                "error",
                error_message=(
                    f"Capability '{tool.capability_id}' is not present in this "
                    "Attempt's immutable snapshot"
                ),
            )

        # 2. Validate before any policy/model decision.
        try:
            validated = self._validate(tool, arguments)
        except ToolValidationError as e:
            return ToolResult(
                tool_call_id,
                tool_name,
                "error",
                error_message=str(e),
            )

        # 3. Deterministic policy evaluation.
        decision = self._policy.evaluate(
            tool_name,
            validated,
            tool,
            agent_mode=context.agent_mode,
        )
        if decision.decision.value == "deny":
            self._emit_event("ToolDenied", tool_name, "denied", decision.reason, 0)
            return ToolResult(
                tool_call_id,
                tool_name,
                "error",
                error_message=f"Policy denied: {decision.reason}",
            )

        if decision.requires_approval:
            return self._require_approval(
                tool_call_id,
                tool,
                validated,
                context,
                decision.reason,
                "policy",
                decision.constraints,
            )

        # 2b. Auto Mode is an additional gate, never an authorization source.
        if self._auto_mode is not None:
            auto_decision = await self._auto_mode.evaluate(tool, validated, context)
            if not auto_decision.is_allowed:
                self._emit_event(
                    "AutoModeBlocked",
                    tool_name,
                    "blocked",
                    f"{auto_decision.source}: {auto_decision.reason}",
                    0,
                )
                return self._require_approval(
                    tool_call_id,
                    tool,
                    validated,
                    context,
                    auto_decision.reason,
                    auto_decision.source,
                    decision.constraints,
                )

        constraints = decision.constraints
        try:
            self._enforce_constraints(tool, validated, constraints)
        except ToolValidationError as exc:
            return ToolResult(
                tool_call_id,
                tool_name,
                "error",
                error_message=str(exc),
                constraints=constraints,
            )
        runtime_context = replace(
            context,
            constraints=constraints,
            tool_call_id=tool_call_id,
        )

        # 3. 持久化 executing + 计算幂等键（副作用 Tool 复用）
        request_hash = _hash_arguments(tool.capability_id, validated)
        arguments_ref = self._store_payload(
            _canonical_arguments(validated).encode("utf-8"),
            content_type="application/json",
            retention_class="secret",
        )
        try:
            self._persist_start(
                tool_call_id,
                context.attempt_id,
                tool_name,
                validated,
                tool_version=tool.version,
                request_hash=request_hash,
                arguments_ref=arguments_ref,
                constraints=constraints,
            )
        except ToolExecutionError as exc:
            return ToolResult(
                tool_call_id,
                tool_name,
                "error",
                error_message=f"Tool intent persistence failed: {exc}",
                constraints=constraints,
            )

        # 5. 执行
        started_at = datetime.now(UTC)
        try:
            raw_result = await asyncio.wait_for(
                tool.handler(validated, runtime_context),
                timeout=constraints.timeout_seconds,
            )
            if isinstance(raw_result, DeferredExecution):
                duration = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
                self._persist_receipt(
                    tool,
                    runtime_context,
                    request_hash=request_hash,
                    status="succeeded",
                    summary=raw_result.summary,
                )
                self._persist_end(
                    tool_call_id,
                    "waiting_external",
                    result=raw_result.summary,
                    request_hash=request_hash,
                )
                return ToolResult(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    status="waiting_external",
                    result=raw_result.summary,
                    duration_ms=duration,
                    constraints=constraints,
                    waiting_id=raw_result.waiting_id,
                )
            duration = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            result_text, payload_ref, raw_size, truncated = self._prepare_output(
                tool,
                raw_result,
                constraints,
            )

            self._persist_receipt(
                tool,
                runtime_context,
                request_hash=request_hash,
                status="succeeded",
                summary=result_text,
            )
            self._persist_end(
                tool_call_id,
                "succeeded",
                result=result_text,
                request_hash=request_hash,
                result_ref=payload_ref,
                trust_label=tool.result_trust_label,
                result_size_bytes=raw_size,
            )
            self._emit_event("ToolExecuted", tool_name, "success", result_text, duration)
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="success",
                result=result_text,
                duration_ms=duration,
                trust_label=tool.result_trust_label,
                payload_ref=payload_ref,
                raw_size_bytes=raw_size,
                truncated=truncated,
                constraints=constraints,
            )
        except Exception as e:
            duration = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            uncertain = isinstance(
                e,
                (TimeoutError, asyncio.TimeoutError, ConnectionError, ToolExecutionError),
            )
            status = "unknown" if uncertain and tool.side_effect_class != "none" else "failed"
            persistence_error = self._record_failed_execution(
                tool_call_id,
                tool,
                runtime_context,
                request_hash=request_hash,
                status=status,
                error=e,
            )
            self._emit_event("ToolExecuted", tool_name, "error", str(e), duration)
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="error",
                error_message=(
                    f"{e}; execution audit failed: {persistence_error}"
                    if persistence_error
                    else str(e)
                ),
                duration_ms=duration,
                constraints=constraints,
            )

    def _require_approval(
        self,
        tool_call_id: str,
        tool: ToolDef,
        arguments: dict[str, Any],
        context: ToolContext,
        reason: str,
        source: str,
        constraints: ConstraintSet | None = None,
    ) -> ToolResult:
        if self._approval_service is None or not context.turn_id:
            prefix = (
                "Auto mode blocked"
                if source not in {"policy", "always"}
                else "Approval required but unavailable"
            )
            return ToolResult(
                tool_call_id,
                tool.name,
                "error",
                error_message=f"{prefix}: {reason}",
            )
        args_hash = _hash_arguments(tool.capability_id, arguments)
        constraints = constraints or ConstraintSet()
        arguments_ref = self._store_payload(
            _canonical_arguments(arguments).encode("utf-8"),
            content_type="application/json",
            retention_class="secret",
        )
        request = {
            "kind": "tool_call",
            "tool_call_id": tool_call_id,
            "tool_name": tool.name,
            "capability_id": tool.capability_id,
            "tool_version": tool.version,
            "tool_schema_hash": _tool_schema_hash(tool),
            "arguments_snapshot_ref": arguments_ref,
            "arguments_summary": _redact_secret_text(_canonical_arguments(arguments))[:2_000],
            "arguments_hash": args_hash,
            "principal_id": context.principal_id,
            "turn_id": context.turn_id,
            "attempt_id": context.attempt_id,
            "policy_version": "2",
            "auto_mode_source": source,
            "reason": reason,
            "permissions": list(tool.permissions),
            "risk_level": tool.risk_level,
            "constraints": constraints.to_dict(),
            "auto_mode_version": "2",
        }
        # Unit/embedded callers may intentionally run without a PayloadStore.
        # Keep the compatibility fallback scoped to that case; production always
        # persists the canonical snapshot and therefore never stores raw arguments
        # in the approval row.
        if not arguments_ref:
            request["arguments"] = arguments
        approval = self._approval_service.find_or_create_tool_approval(
            turn_id=context.turn_id,
            request=request,
        )
        self._emit_event("ToolApprovalRequired", tool.name, "waiting", reason, 0)
        return ToolResult(
            tool_call_id,
            tool.name,
            "approval_required",
            error_message=reason,
            approval_id=approval.approval_id,
            constraints=constraints,
        )

    async def resume_approved(self, context: ToolContext) -> ToolResult | None:
        """Execute the exact approved call before the resumed model iteration."""
        if self._approval_service is None or not context.turn_id:
            return None
        request = self._approval_service.claim_approved_tool_call(context.turn_id)
        if request is None:
            return None
        tool = self._registry.get(str(request.get("tool_name", "")))
        arguments = self._load_arguments(request)
        constraints = ConstraintSet.from_dict(request.get("constraints"))
        if (
            tool is None
            or tool.version != request.get("tool_version")
            or _tool_schema_hash(tool) != request.get("tool_schema_hash")
            or _hash_arguments(tool.capability_id, arguments) != request.get("arguments_hash")
            or request.get("policy_version") != "2"
            or request.get("auto_mode_version") != "2"
        ):
            invalidate = getattr(
                self._approval_service,
                "invalidate_approved_tool_call",
                None,
            )
            if invalidate is not None:
                invalidate(
                    str(request.get("approval_id", "")),
                    int(request.get("approval_version", 0)),
                )
            if tool is not None:
                refreshed = self._policy.evaluate(
                    tool.name,
                    arguments,
                    tool,
                    agent_mode=context.agent_mode,
                )
                return self._require_approval(
                    str(request.get("tool_call_id", "")),
                    tool,
                    arguments,
                    context,
                    "Approved tool call became stale and must be approved again",
                    "policy",
                    refreshed.constraints,
                )
            return ToolResult(
                str(request.get("tool_call_id", "")),
                str(request.get("tool_name", "")),
                "error",
                error_message="Approved capability no longer exists",
            )
        # Approval never overrides a deterministic deny.
        policy = self._policy.evaluate(
            tool.name,
            arguments,
            tool,
            agent_mode=context.agent_mode,
        )
        if policy.decision.value == "deny":
            self._invalidate_approval(request)
            return ToolResult(
                str(request.get("tool_call_id", "")),
                tool.name,
                "error",
                error_message=f"Policy denied after approval: {policy.reason}",
            )

        if policy.constraints.intersect(constraints) != constraints:
            self._invalidate_approval(request)
            return self._require_approval(
                str(request.get("tool_call_id", "")),
                tool,
                arguments,
                context,
                "Approved constraints became stale and must be approved again",
                "policy",
                policy.constraints,
            )
        try:
            self._enforce_constraints(tool, arguments, constraints)
        except ToolValidationError as exc:
            return ToolResult(
                str(request.get("tool_call_id", "")),
                tool.name,
                "error",
                error_message=str(exc),
            )
        consume = getattr(self._approval_service, "consume_approved_tool_call", None)
        if consume is None or not consume(
            str(request.get("approval_id", "")),
            int(request.get("approval_version", 0)),
        ):
            return ToolResult(
                str(request.get("tool_call_id", "")),
                tool.name,
                "error",
                error_message="Approval was expired or concurrently consumed",
            )
        runtime_context = replace(
            context,
            constraints=constraints,
            tool_call_id=str(request.get("tool_call_id", "")),
        )
        self._persist_start(
            str(request.get("tool_call_id", "")),
            context.attempt_id,
            tool.name,
            arguments,
            tool_version=tool.version,
            request_hash=str(request.get("arguments_hash", "")),
            arguments_ref=str(request.get("arguments_snapshot_ref", "")),
            constraints=constraints,
        )
        started_at = datetime.now(UTC)
        try:
            raw = await asyncio.wait_for(
                tool.handler(arguments, runtime_context),
                timeout=constraints.timeout_seconds,
            )
            text, payload_ref, raw_size, truncated = self._prepare_output(
                tool,
                raw,
                constraints,
            )
            duration = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            self._persist_receipt(
                tool,
                runtime_context,
                request_hash=str(request.get("arguments_hash", "")),
                status="succeeded",
                summary=text,
            )
            self._persist_end(
                str(request.get("tool_call_id", "")),
                "succeeded",
                text,
                result_ref=payload_ref,
                trust_label=tool.result_trust_label,
                result_size_bytes=raw_size,
            )
            return ToolResult(
                str(request.get("tool_call_id", "")),
                tool.name,
                "success",
                result=text,
                duration_ms=duration,
                trust_label=tool.result_trust_label,
                approval_id=str(request.get("approval_id", "")),
                payload_ref=payload_ref,
                raw_size_bytes=raw_size,
                truncated=truncated,
                constraints=constraints,
            )
        except Exception as exc:
            uncertain = isinstance(
                exc,
                (TimeoutError, asyncio.TimeoutError, ConnectionError, ToolExecutionError),
            )
            status = "unknown" if uncertain and tool.side_effect_class != "none" else "failed"
            persistence_error = self._record_failed_execution(
                str(request.get("tool_call_id", "")),
                tool,
                runtime_context,
                request_hash=str(request.get("arguments_hash", "")),
                status=status,
                error=exc,
            )
            return ToolResult(
                str(request.get("tool_call_id", "")),
                tool.name,
                "error",
                error_message=(
                    f"{exc}; execution audit failed: {persistence_error}"
                    if persistence_error
                    else str(exc)
                ),
                approval_id=str(request.get("approval_id", "")),
                constraints=constraints,
            )

    def claim_deferred_result(self, context: ToolContext) -> ToolResult | None:
        claim = getattr(self._sink, "claim_deferred_result", None) if self._sink else None
        if claim is None or not context.turn_id:
            return None
        raw = claim(context.turn_id)
        if not raw:
            return None
        return ToolResult(
            tool_call_id=str(raw.get("tool_call_id", "")),
            tool_name=str(raw.get("tool_name", "delegate_task")),
            status="success",
            result=str(raw.get("result", "")),
            trust_label="verified",
            waiting_id=str(raw.get("waiting_id", "")),
        )

    def _invalidate_approval(self, request: dict[str, Any]) -> None:
        invalidate = getattr(
            self._approval_service,
            "invalidate_approved_tool_call",
            None,
        )
        if invalidate is not None:
            invalidate(
                str(request.get("approval_id", "")),
                int(request.get("approval_version", 0)),
            )

    async def execute_many(
        self,
        calls: list[ToolCallState],
        context: ToolContext,
    ) -> list[ToolResult]:
        """Execute in order and stop at the first approval boundary."""
        final: list[ToolResult] = []
        for call in calls:
            try:
                result = await self.execute(
                    call.tool_call_id,
                    call.tool_name,
                    call.arguments,
                    context,
                )
            except Exception as exc:
                result = ToolResult(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    status="error",
                    error_message=str(exc),
                )
            final.append(result)
            if result.status == "approval_required":
                break
        return final

    # ── 持久化 ──

    def _persist_start(
        self,
        tool_call_id: str,
        attempt_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_version: str = "1.0",
        request_hash: str = "",
        arguments_ref: str = "",
        constraints: ConstraintSet | None = None,
    ) -> None:
        if not self._sink:
            return

        try:
            self._sink.insert(
                {
                    "tool_call_id": tool_call_id,
                    "attempt_id": attempt_id,
                    "attempt_type": "run",
                    "tool_name": tool_name,
                    "tool_version": tool_version,
                    "arguments": _redact_secret_text(_canonical_arguments(arguments))[:2_000],
                    "arguments_ref": arguments_ref,
                    "constraints_json": json.dumps(
                        (constraints or ConstraintSet()).to_dict(),
                        ensure_ascii=False,
                    ),
                    "idempotency_key": request_hash,
                    "status": "executing",
                    "started_at": epoch_ms(datetime.now(UTC)),
                }
            )
        except Exception as exc:
            raise ToolExecutionError("cannot persist ToolCall intent") from exc

    def _persist_end(
        self,
        tool_call_id: str,
        status: str,
        result: str = "",
        request_hash: str = "",
        result_ref: str = "",
        trust_label: str = "unverified",
        result_size_bytes: int = 0,
    ) -> None:
        if not self._sink:
            return
        try:
            self._sink.insert(
                {
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "result_summary": result[:500] if result else "",
                    "result_ref": result_ref,
                    "result_trust_label": trust_label,
                    "result_size_bytes": result_size_bytes,
                    "completed_at": epoch_ms(datetime.now(UTC)),
                }
            )
        except Exception as exc:
            raise ToolExecutionError("cannot persist ToolCall result") from exc

    def _enqueue_reconcile(
        self,
        tool_call_id: str,
        tool: ToolDef,
        context: ToolContext,
        request_hash: str,
        receipt_id: str,
    ) -> None:
        enqueue = getattr(self._sink, "enqueue_reconcile", None) if self._sink else None
        if enqueue is None:
            return
        try:
            enqueue(
                {
                    "capability_id": tool.capability_id,
                    "tool_call_id": tool_call_id,
                    "attempt_id": context.attempt_id,
                    "request_hash": request_hash,
                    "receipt_id": receipt_id,
                }
            )
        except Exception as exc:
            raise ToolExecutionError("cannot enqueue reconciliation") from exc

    def _persist_receipt(
        self,
        tool: ToolDef,
        context: ToolContext,
        *,
        request_hash: str,
        status: str,
        summary: str,
    ) -> str:
        if tool.side_effect_class == "none" or not self._sink:
            return ""
        insert_receipt = getattr(self._sink, "insert_receipt", None)
        if insert_receipt is None:
            return ""
        try:
            return str(
                insert_receipt(
                    {
                        "capability_id": tool.capability_id,
                        "operation_id": context.tool_call_id,
                        "request_hash": request_hash,
                        "side_effect_class": tool.side_effect_class,
                        "status": status,
                        "reconcile_status": ("pending" if status == "unknown" else "not_needed"),
                        "summary": summary,
                        "attempt_id": context.attempt_id,
                        "created_at": epoch_ms(datetime.now(UTC)),
                    }
                )
            )
        except Exception as exc:
            raise ToolExecutionError("cannot persist side-effect receipt") from exc

    def _record_failed_execution(
        self,
        tool_call_id: str,
        tool: ToolDef,
        context: ToolContext,
        *,
        request_hash: str,
        status: str,
        error: Exception,
    ) -> str:
        """Persist failure without masking the original execution exception.

        A persistence failure is still surfaced to the caller.  Secondary writes
        are attempted independently so one broken receipt sink does not prevent
        the ToolCall row from becoming terminal (or ``unknown``).
        """
        failures: list[str] = []
        receipt_id = ""
        try:
            receipt_id = self._persist_receipt(
                tool,
                context,
                request_hash=request_hash,
                status=status,
                summary=str(error),
            )
        except ToolExecutionError as exc:
            failures.append(str(exc))
        try:
            self._persist_end(tool_call_id, status)
        except ToolExecutionError as exc:
            failures.append(str(exc))
        if status == "unknown" and receipt_id:
            try:
                self._enqueue_reconcile(
                    tool_call_id,
                    tool,
                    context,
                    request_hash,
                    receipt_id,
                )
            except ToolExecutionError as exc:
                failures.append(str(exc))
        return "; ".join(failures)

    # ── 参数校验 ──

    def _validate(
        self,
        tool: ToolDef,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """使用 JSON Schema 校验参数。"""
        schema = tool.input_schema
        for field_name in schema.get("required", []):
            if field_name not in arguments:
                raise ToolValidationError(
                    f"Tool '{tool.name}': validation error — missing required "
                    f"parameter '{field_name}'"
                )
        try:
            from jsonschema import Draft202012Validator

            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(arguments)
        except Exception as e:
            raise ToolValidationError(f"Tool '{tool.name}': validation error — {e}") from e

        return arguments

    def _enforce_constraints(
        self,
        tool: ToolDef,
        arguments: dict[str, Any],
        constraints: ConstraintSet,
    ) -> None:
        path = arguments.get("path") or arguments.get("cwd")
        if path and constraints.allowed_paths:
            normalized = str(path).replace("\\", "/").casefold().rstrip("/") or "."
            allowed = tuple(
                value.replace("\\", "/").casefold().rstrip("/") or "."
                for value in constraints.allowed_paths
            )
            if not any(
                scope == "." or normalized == scope or normalized.startswith(scope + "/")
                for scope in allowed
            ):
                raise ToolValidationError("Policy constraints reject the requested path")
        if "content" in arguments:
            size = len(str(arguments["content"]).encode("utf-8"))
            if size > constraints.max_write_bytes:
                raise ToolValidationError("Policy constraints reject the write size")
        for field in ("patch", "new_string", "new_text"):
            if field in arguments:
                size = len(str(arguments[field]).encode("utf-8"))
                if size > constraints.max_write_bytes:
                    raise ToolValidationError("Policy constraints reject the write size")
        if "filesystem.write" in tool.permissions and constraints.mount_mode != "rw":
            raise ToolValidationError("Policy constraints do not grant workspace write access")
        if "filesystem.read" in tool.permissions and constraints.mount_mode == "none":
            raise ToolValidationError("Policy constraints do not grant workspace read access")
        if tool.name == "web_fetch" and not constraints.network_enabled:
            raise ToolValidationError("Policy constraints disable network access")

    def _prepare_output(
        self,
        tool: ToolDef,
        result: Any,
        constraints: ConstraintSet,
    ) -> tuple[str, str, int, bool]:
        if isinstance(result, (dict, list)):
            structured = result
            text = json.dumps(result, ensure_ascii=False)
        else:
            text = str(result)
            try:
                structured = json.loads(text)
            except (TypeError, ValueError) as exc:
                accepts_text = bool(
                    tool.output_schema
                    and (
                        tool.output_schema.get("type") == "string"
                        or "string" in tool.output_schema.get("type", [])
                    )
                )
                structured = text if accepts_text else None
                if tool.output_schema and not accepts_text:
                    raise ToolValidationError(
                        f"Tool '{tool.name}': output is not valid JSON",
                    ) from exc
        if tool.output_schema:
            from jsonschema import Draft202012Validator

            Draft202012Validator.check_schema(tool.output_schema)
            Draft202012Validator(tool.output_schema).validate(structured)
        if isinstance(structured, (dict, list)):
            item_count = _count_result_items(structured)
            if item_count > constraints.max_result_items:
                raise ToolValidationError(
                    f"Tool result has {item_count} items; limit is {constraints.max_result_items}"
                )
        clean = _redact_secret_text(text)
        raw = clean.encode("utf-8")
        payload_ref = ""
        truncated = len(clean) > constraints.max_output_chars
        if truncated:
            payload_ref = self._store_payload(
                raw,
                content_type="application/json" if structured is not None else "text/plain",
            )
            clean = (
                clean[: constraints.max_output_chars]
                + f"\n... (truncated, payload_ref={payload_ref}, {len(raw)} bytes)"
            )
        return clean, payload_ref, len(raw), truncated

    def _store_payload(
        self,
        data: bytes,
        *,
        content_type: str,
        retention_class: str = "hot",
    ) -> str:
        if self._payload_store is None:
            return ""
        obj = self._payload_store.put(
            data,
            content_type=content_type,
            retention_class=retention_class,
        )
        return str(getattr(obj, "payload_id", ""))

    def _load_arguments(self, request: dict[str, Any]) -> dict[str, Any]:
        ref = str(request.get("arguments_snapshot_ref", ""))
        if ref and self._payload_store is not None:
            raw = self._payload_store.get(ref)
            if raw is None:
                return {}
            return json.loads(raw.decode("utf-8"))
        return dict(request.get("arguments") or {})

    # ── 输出截断 ──

    @staticmethod
    def _truncate_output(text: str) -> str:
        if len(text) > MAX_TOOL_OUTPUT_CHARS:
            return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n... (truncated, {len(text)} chars)"
        return text

    # ── 事件发布（D8） ──

    def _emit_event(
        self,
        event_type: str,
        tool_name: str,
        status: str,
        summary: str,
        duration_ms: int,
    ) -> None:
        if not self._on_event:
            return
        try:
            self._on_event(
                {
                    "event_type": event_type,
                    "tool_name": tool_name,
                    "status": status,
                    "summary": summary[:200],
                    "duration_ms": duration_ms,
                }
            )
        except Exception:
            pass

    # ── 结果格式化 ──

    @staticmethod
    def format_tool_message(
        tool_call_id: str,
        result: ToolResult,
    ) -> dict[str, Any]:
        """格式化为 tool role 消息，供下一轮模型请求使用。"""
        content = (
            result.result
            if result.status == "success"
            else (
                f"Approval required ({result.approval_id}): {result.error_message}"
                if result.status == "approval_required"
                else f"Error: {result.error_message}"
            )
        )
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
            "trust_label": result.trust_label,
        }

    @staticmethod
    def format_tool_results(
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        """批量格式化 tool result 消息。"""
        return [ToolExecutor.format_tool_message(r.tool_call_id, r) for r in results]


def _hash_arguments(tool_name: str, arguments: dict[str, Any]) -> str:
    """计算副作用幂等键 hash（稳定序列化 + sha256）。"""
    import hashlib

    canonical = _canonical_arguments(arguments)
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


def _tool_schema_hash(tool: ToolDef) -> str:
    import hashlib

    canonical = json.dumps(
        {"input": tool.input_schema, "output": tool.output_schema},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _canonical_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(
        arguments,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _count_result_items(value: Any) -> int:
    if isinstance(value, dict):
        return len(value) + sum(_count_result_items(item) for item in value.values())
    if isinstance(value, list):
        return len(value) + sum(_count_result_items(item) for item in value)
    return 0


_SECRET_PATTERNS = (
    re.compile(r'(?i)("?(?:api[_-]?key|token|secret|password)"?\s*[:=]\s*)"?[^",\s}]+'),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
)


def _redact_secret_text(value: str) -> str:
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: match.group(1) + '"<redacted>"' if match.lastindex else "<redacted>", text
        )
    return text
