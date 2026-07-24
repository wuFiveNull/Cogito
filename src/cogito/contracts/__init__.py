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
from .context import (
    ContextBuilder as ContextBuilder,
)
from .context import (
    ContextItem as ContextItem,
)
from .context import (
    ContextSnapshot as ContextSnapshot,
)
from .context import (
    estimate_tokens as estimate_tokens,
)
from .envelope import (
    AgentReply,
    AgentRequest,
    ChannelEnvelope,
    CommandEnvelope,
    ErrorCategory,
    ErrorEnvelope,
    ReplyMode,
    ReplyRoute,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from .memory import MemoryReader as MemoryReader
from .memory import MemoryWriter as MemoryWriter
from .tool_call import ToolCallSink as ToolCallSink
from .trace_context import TraceContext
from .event_query import EventCursorError, EventPayloadUnavailableError

__all__ = [
    "TraceContext",
    "EventCursorError",
    "EventPayloadUnavailableError",
    "ChannelEnvelope",
    "ReplyRoute",
    "AgentRequest",
    "AgentReply",
    "ReplyMode",
    "ToolRequest",
    "ToolResult",
    "ToolStatus",
    "ErrorEnvelope",
    "ErrorCategory",
    "CommandEnvelope",
    # PLAN-09 M2: shared time contract
    "Clock",
    "ProductionClock",
    "FakeClock",
    "epoch_ms",
    "from_epoch_ms",
    "now_ms",
    "iso_to_epoch_ms",
    "EPOCH",
]
