"""Now tool — 返回当前日期和时间。

零依赖内置工具。
"""

from __future__ import annotations

from datetime import UTC, datetime

from cogito.capability.models import ToolContext, ToolDef

TOOL_NAME = "now"


async def handler(args: dict, context: ToolContext) -> str:
    """返回当前 UTC 时间和时区信息。"""
    now = datetime.now(UTC)
    return (
        f"Current UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Timezone: UTC\n"
        f"Unix timestamp: {int(now.timestamp())}"
    )


tool_def = ToolDef(
    name=TOOL_NAME,
    description="Get the current date, time, and timezone information.",
    input_schema={
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": "Optional output format: 'iso', 'unix', or 'human' (default).",
                "enum": ["human", "iso", "unix"],
            },
        },
    },
    toolset=("core",),
    handler=handler,
    risk_level="low",
)
