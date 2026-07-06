"""Contracts — 跨模块/跨进程契约。"""

from .envelope import (
    AgentReply,
    AgentRequest,
    ChannelEnvelope,
    CommandEnvelope,
    ErrorCategory,
    ErrorEnvelope,
    EventEnvelope,
    ReplyMode,
    ReplyRoute,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from .trace_context import TraceContext

__all__ = [
    "TraceContext",
    "ChannelEnvelope", "ReplyRoute",
    "AgentRequest", "AgentReply", "ReplyMode",
    "ToolRequest", "ToolResult", "ToolStatus",
    "ErrorEnvelope", "ErrorCategory",
    "CommandEnvelope",
    "EventEnvelope",
]
