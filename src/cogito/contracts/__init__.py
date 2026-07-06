"""Contracts — 跨模块/跨进程契约。"""

from .trace_context import TraceContext
from .envelope import (
    ChannelEnvelope, ReplyRoute,
    AgentRequest, AgentReply, ReplyMode,
    ToolRequest, ToolResult, ToolStatus,
    ErrorEnvelope, ErrorCategory,
    CommandEnvelope,
    EventEnvelope,
)

__all__ = [
    "TraceContext",
    "ChannelEnvelope", "ReplyRoute",
    "AgentRequest", "AgentReply", "ReplyMode",
    "ToolRequest", "ToolResult", "ToolStatus",
    "ErrorEnvelope", "ErrorCategory",
    "CommandEnvelope",
    "EventEnvelope",
]
