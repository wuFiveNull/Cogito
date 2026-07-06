"""Echo tool — 回显输入参数。

最基本的工具，用于验证 Tool Calling 整体链路。
始终可用，零依赖。
"""

from __future__ import annotations

from cogito.capability.models import ToolDef, ToolContext

TOOL_NAME = "echo"


async def handler(args: dict, context: ToolContext) -> str:
    """返回输入文本。"""
    return args.get("text", "")


tool_def = ToolDef(
    name=TOOL_NAME,
    description="Echo back the input text. Use this to verify tool calling works.",
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to echo back.",
            },
        },
        "required": ["text"],
    },
    toolset=("core",),
    handler=handler,
    risk_level="low",
)
