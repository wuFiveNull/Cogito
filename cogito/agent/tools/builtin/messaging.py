# cogito/agent/tools/builtin/messaging.py
#
# Built-in tool: send_message — sends a message through a channel.

from __future__ import annotations

from typing import Mapping

from cogito.agent.domain.tools import (
    ToolConcurrencyMode,
    ToolDefinition,
    ToolKind,
    ToolLimits,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)


class SendMessageHandler:
    """Handler for send_message — sends a message through the channel."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="send_message",
            description="Send a message to the user. Use this to proactively send information or ask questions.",
            input_schema={
                "type": "object", "properties": {
                    "message": {"type": "string", "minLength": 1, "description": "Message to send"},
                },
                "required": ["message"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.EXTERNAL_MUTATION, risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=10.0, idempotent=True, parallel_safe=False,
            kind=ToolKind.COMMUNICATE, risk=ToolRisk.EXTERNAL_WRITE,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_SESSION,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        return {"note": "send_message requires a channel connection"}
