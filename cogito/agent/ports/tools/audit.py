# cogito/agent/ports/tools/audit.py
#
# Tool Audit Port — append-only recording of tool execution events.
#
# Design rules (see tool-system-spec §16.3):
#   - Audit records are append-only.
#   - Raw arguments are never stored; use hash + redacted summary.
#   - External writes must record target, approval ID, and result.
#   - Audit write failure: fail-closed for high-risk, fail-open for read-only.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ToolAuditRecord:
    call_id: str
    turn_id: str
    tool_name: str
    actor_id: str
    session_id: str
    status: str
    risk: str
    started_at: datetime
    duration_ms: int
    arguments_hash: str
    approval_id: str | None = None
    error_code: str | None = None
    policy_reason_code: str | None = None
    artifact_ids: tuple[str, ...] = ()


class ToolAuditPort(Protocol):
    """Append-only audit trail for tool executions."""

    async def record(self, record: ToolAuditRecord) -> None:
        ...

    async def record_batch(
        self,
        records: tuple[ToolAuditRecord, ...],
    ) -> None:
        ...
