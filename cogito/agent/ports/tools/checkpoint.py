# cogito/agent/ports/tools/checkpoint.py
#
# Tool Loop Checkpoint Port — serialisable agent-loop state snapshots.
#
# Design rules (see tool-system-spec §14.4):
#   - Checkpoints enable durable approval: save → wait → restore → resume.
#   - Checkpoints are fully serialisable (no SDK/DB/EventSink refs).
#   - Integrity hash covers actor, session, approval ID, call IDs + args.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LoopCheckpointRecord:
    checkpoint_id: str
    turn_id: str
    approval_id: str
    serialised_state: bytes
    integrity_hash: str
    created_at: datetime
    expires_at: datetime | None = None


class ToolLoopCheckpointPort(Protocol):
    """Persistent checkpoint storage for approval suspend/resume."""

    async def save(
        self,
        record: LoopCheckpointRecord,
    ) -> None:
        ...

    async def load(
        self,
        checkpoint_id: str,
    ) -> LoopCheckpointRecord | None:
        ...

    async def delete(self, checkpoint_id: str) -> bool:
        ...

    async def cleanup_expired(self) -> int:
        ...
