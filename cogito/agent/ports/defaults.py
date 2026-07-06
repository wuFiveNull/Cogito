# cogito/agent/ports/defaults.py
#
# Default/stub implementations for agent runtime ports.
#
# These are minimal working implementations for development and testing.
# Replace them with production implementations as each subsystem matures.
#
# Provided implementations:
#   - SystemClock          → ClockPort
#   - Uuid7Generator       → IdGeneratorPort
#   - DefaultModelContextWindow → ModelContextWindowPort
#   - DefaultToolRegistry  → ToolRegistryPort
#   - DefaultToolPolicy    → ToolPolicyPort
#   - DefaultToolExecutor  → ToolExecutorPort

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from cogito.agent.domain.messages import ModelMessage
from cogito.agent.domain.tools import (
    PreparedToolCall,
    ToolDefinition,
    ToolExecutionResult,
    ToolExecutionStatus,
)
from cogito.agent.ports.model_context import ContextWindowRequest, ModelContextWindowPort
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

# ═══════════════════════════════════════════════════════════════════════
# ClockPort
# ═══════════════════════════════════════════════════════════════════════


class SystemClock:
    """Real system clock — returns current UTC time."""

    def now(self) -> datetime:
        return datetime.now()


# ═══════════════════════════════════════════════════════════════════════
# IdGeneratorPort
# ═══════════════════════════════════════════════════════════════════════


class Uuid7Generator:
    """UUIDv7-based ID generator."""

    def new_id(self) -> str:
        from cogito.database.ids import new_uuid

        return new_uuid()


# ═══════════════════════════════════════════════════════════════════════
# ModelContextWindowPort
# ═══════════════════════════════════════════════════════════════════════


class DefaultModelContextWindow:
    """Minimal context window — returns messages as-is.

    This implementation does not truncate or compress messages.  It serves
    as a placeholder until a token-aware fit implementation is wired.
    For most models with large context windows this will work fine for
    typical conversation lengths.
    """

    async def fit(
        self,
        request: ContextWindowRequest,
    ) -> tuple[ModelMessage, ...]:
        return request.messages


# ═══════════════════════════════════════════════════════════════════════
# ToolRegistryPort
# ═══════════════════════════════════════════════════════════════════════


class DefaultToolRegistry:
    """Simple tool registry with name resolution and JSON Schema validation.

    ``resolve`` matches the tool name exactly against available_tools.
    ``validate_arguments`` validates the arguments against the tool's
    input_schema using basic type checks.
    """

    def resolve(
        self,
        *,
        name: str,
        available_tools: tuple[ToolDefinition, ...],
    ) -> ToolDefinition | None:
        for tool in available_tools:
            if tool.name == name:
                return tool
        return None

    def validate_arguments(
        self,
        *,
        definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> None:
        """Basic validation: checks required fields and types.

        A production implementation should use a JSON Schema library
        (e.g. ``jsonschema``) for full schema validation.
        """
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")

        schema = definition.input_schema
        if not schema:
            return

        required = schema.get("required", [])
        properties = schema.get("properties", {})

        # Check required fields
        for field in required:
            if field not in arguments or arguments[field] is None:
                raise ValueError(f"Missing required argument: {field}")

        # Check field types (basic)
        for field, value in arguments.items():
            prop = properties.get(field)
            if prop is None:
                continue
            expected_type = prop.get("type")
            if expected_type == "string" and not isinstance(value, str):
                raise ValueError(
                    f"Argument '{field}' must be a string, got {type(value).__name__}",
                )
            if expected_type == "integer" and not isinstance(value, int):
                raise ValueError(
                    f"Argument '{field}' must be an integer, got {type(value).__name__}",
                )
            if expected_type == "number" and not isinstance(value, (int, float)):
                raise ValueError(
                    f"Argument '{field}' must be a number, got {type(value).__name__}",
                )
            if expected_type == "boolean" and not isinstance(value, bool):
                raise ValueError(
                    f"Argument '{field}' must be a boolean, got {type(value).__name__}",
                )


# ═══════════════════════════════════════════════════════════════════════
# ToolPolicyPort
# ═══════════════════════════════════════════════════════════════════════


class DefaultToolPolicy:
    """Permissive policy — ALLOW all tool calls.

    A production policy should evaluate risk level, side effects,
    user preferences, and session context.
    """

    async def evaluate(
        self,
        *,
        actor_id: str,
        session_id: str,
        prepared_call: PreparedToolCall,
    ) -> ToolPolicyDecision:
        return ToolPolicyDecision(
            decision=ToolPolicyDecisionType.ALLOW,
            reason_code="DEFAULT_ALLOW",
            safe_message="Tool call is allowed by default policy.",
        )


# ═══════════════════════════════════════════════════════════════════════
# ToolExecutorPort
# ═══════════════════════════════════════════════════════════════════════


class DefaultToolExecutor:
    """Stub tool executor — returns a descriptive result for any tool.

    This allows the AgentLoop to complete a turn even when no real
    tools are registered.  The model receives a message explaining
    that the tool is not yet implemented.
    """

    async def execute(
        self,
        *,
        prepared_call: PreparedToolCall,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=prepared_call.call.call_id,
            tool_name=prepared_call.call.tool_name,
            status=ToolExecutionStatus.FAILED,
            model_content=json.dumps(
                {
                    "error": {
                        "code": "TOOL_NOT_IMPLEMENTED",
                        "message": (
                            f"Tool '{prepared_call.call.tool_name}' is not "
                            f"implemented in this environment."
                        ),
                    }
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            safe_message=f"Tool '{prepared_call.call.tool_name}' is not available.",
            error_code="TOOL_NOT_IMPLEMENTED",
            retryable=False,
        )


__all__ = [
    "DefaultModelContextWindow",
    "DefaultToolExecutor",
    "DefaultToolPolicy",
    "DefaultToolRegistry",
    "SystemClock",
    "Uuid7Generator",
]
