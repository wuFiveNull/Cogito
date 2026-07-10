"""Contracts — 跨模块/跨进程契约。"""

from .clock import (
    EPOCH,
    Clock,
    FakeClock,
    ProductionClock,
    epoch_ms,
    from_epoch_ms,
    iso_to_epoch_ms,
    now_ms,
)
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
    # PLAN-09 M2: shared time contract
    "Clock", "ProductionClock", "FakeClock",
    "epoch_ms", "from_epoch_ms", "now_ms", "iso_to_epoch_ms", "EPOCH",
]
