# cogito/agent/ports/tracing.py

from __future__ import annotations

from typing import Protocol


class RuntimeTracePort(Protocol):
    """Distributed tracing interface for the runtime."""

    async def start_turn(
        self,
        *,
        turn_id: str,
        request_id: str,
    ) -> str:
        """Start a trace span for a turn. Returns a trace_id."""
        ...

    async def end_turn(
        self,
        *,
        trace_id: str,
        status: str,
    ) -> None:
        """End the trace span for a turn."""
        ...
