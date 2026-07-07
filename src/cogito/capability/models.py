"""Tool 领域模型。

CAPABILITY-PLUGINS / 4. Capability Registry 记录格式：
- name, version, toolset, schema, permissions, risk_level, check_fn, supported_modes
- Agent 只能看到当前 Principal、运行模式和 Policy 允许的 Capability 子集。

不在此模块处理序列化、校验或执行逻辑。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

# ── Tool 定义（注册后不可变）──


@dataclass(frozen=True)
class ToolDef:
    """注册表中的不可变工具定义。

    一个 ToolDef 表示一项原子执行能力。
    同一 Tool 可属于多个 toolset。
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    # handler 签名：async def handler(args: dict, context: ToolContext) -> str
    handler: Callable[..., Any] = field(compare=False, hash=False)

    toolset: tuple[str, ...] = ("core",)
    check_fn: Callable[[], bool] | None = field(
        default=None, compare=False, hash=False,
    )
    requires_env: tuple[str, ...] = ()
    risk_level: Literal["low", "medium", "high"] = "low"
    supported_modes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "toolset", tuple(self.toolset))
        object.__setattr__(self, "requires_env", tuple(self.requires_env))
        object.__setattr__(self, "supported_modes", tuple(self.supported_modes))


# ── Tool 调用状态（运行时短生命周期）──


@dataclass
class ToolCallState:
    """一次 Tool 调用的运行时状态。

    与数据库 tool_calls 表对应，但不包含持久化逻辑。
    """

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: Literal["pending", "running", "success", "error"] = "pending"
    result: str = ""
    error_message: str = ""
    started_at: int = 0
    completed_at: int = 0


# ── Tool 执行结果（供格式化用）──


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果，用于格式化为 model 消息。

    实现 TOOL-SANDBOX / 10. 输出：
    - 大型结果写 Payload
    - 给模型的内容使用裁剪摘要
    """

    tool_call_id: str
    tool_name: str
    status: Literal["success", "error"]
    result: str = ""
    error_message: str = ""
    duration_ms: int = 0


# ── 执行上下文 ──


@dataclass(frozen=True)
class ToolContext:
    """提供给工具 handler 的上下文。

    不允许 handler 通过此上下文访问数据库。
    """

    attempt_id: str
    trace_id: str
    tool_call_id: str
    principal_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    input_message_id: str = ""
    conversation_id: str = ""
