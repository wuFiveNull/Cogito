# cogito/agent/tools/builtin/time_tool.py
#
# Built-in tool: get_current_time — returns current date/time information.

from __future__ import annotations

from datetime import datetime, timezone
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
from cogito.agent.ports.tools.registry import ToolHandler


class GetCurrentTimeHandler:
    """Handler for get_current_time — returns the current date and time."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_current_time",
            description="Get the current date and time. Returns the current date, time, timezone, and weekday.",
            input_schema={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "Optional IANA timezone (e.g., 'Asia/Shanghai', 'America/New_York')",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["iso", "unix", "full"],
                        "description": "Output format: iso (default), unix (timestamp), or full (detailed)",
                    },
                },
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=3.0,
            idempotent=True,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
            limits=ToolLimits(timeout_seconds=3.0, max_result_chars=500),
            always_visible=True,
        )

    async def execute(
        self,
        *,
        arguments: Mapping[str, object],
        context: Mapping[str, object],
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        output_format = str(arguments.get("format", "iso"))

        result = {
            "datetime_utc": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
            "unix_timestamp": int(now.timestamp()),
            "utc_offset": "+00:00",
        }

        if output_format == "unix":
            return {"timestamp": int(now.timestamp())}

        if output_format == "full":
            result["microsecond"] = now.microsecond
            result["iso_week"] = now.strftime("%V")
            result["day_of_year"] = now.timetuple().tm_yday

        return result
