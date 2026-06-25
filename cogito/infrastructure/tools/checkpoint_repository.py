# cogito/infrastructure/tools/checkpoint_repository.py
#
# SQLiteLoopCheckpointRepository — persistent checkpoint storage for approvals.
#
# Design rules (see tool-system-spec §14.4):
#   - Checkpoints are serialised bytes + integrity hash.
#   - Checkpoint records have TTL and are periodically cleaned up.
#   - Uses a dedicated ``tool_loop_checkpoints`` table.

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from cogito.agent.ports.tools.checkpoint import LoopCheckpointRecord, ToolLoopCheckpointPort
from cogito.database.connection import AsyncDatabase

logger = logging.getLogger(__name__)


# ── DDL ───────────────────────────────────────────────────────────────────

CREATE_CHECKPOINTS_TABLE = """
CREATE TABLE IF NOT EXISTS tool_loop_checkpoints (
    checkpoint_id    TEXT PRIMARY KEY,
    turn_id          TEXT NOT NULL,
    approval_id      TEXT NOT NULL,
    serialised_state TEXT NOT NULL,
    integrity_hash   TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    expires_at       TEXT,
    UNIQUE(approval_id)
) STRICT;
"""

CHECKPOINT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_turn "
    "ON tool_loop_checkpoints(turn_id);",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_expires "
    "ON tool_loop_checkpoints(expires_at) WHERE expires_at IS NOT NULL;",
]


class SQLiteLoopCheckpointRepository:
    """SQLite-backed checkpoint storage for approval suspend/resume."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def ensure_schema(self) -> None:
        """Create the checkpoints table if it doesn't exist."""
        await self._db.executescript(CREATE_CHECKPOINTS_TABLE)
        for idx in CHECKPOINT_INDEXES:
            try:
                await self._db.execute(idx)
            except Exception:
                pass

    async def save(self, record: LoopCheckpointRecord) -> None:
        await self._db.execute(
            """INSERT OR REPLACE INTO tool_loop_checkpoints
               (checkpoint_id, turn_id, approval_id, serialised_state,
                integrity_hash, created_at, expires_at)
               VALUES (:cid, :tid, :aid, :state, :hash, :created, :expires)""",
            {
                "cid": record.checkpoint_id,
                "tid": record.turn_id,
                "aid": record.approval_id,
                "state": record.serialised_state.decode("utf-8") if isinstance(record.serialised_state, bytes) else record.serialised_state,
                "hash": record.integrity_hash,
                "created": record.created_at.isoformat(),
                "expires": record.expires_at.isoformat() if record.expires_at else None,
            },
        )

    async def load(self, checkpoint_id: str) -> LoopCheckpointRecord | None:
        row = await self._db.fetchone(
            "SELECT * FROM tool_loop_checkpoints WHERE checkpoint_id = :cid",
            {"cid": checkpoint_id},
        )
        if row is None:
            return None
        return self._row_to_record(row)

    async def load_by_approval(self, approval_id: str) -> LoopCheckpointRecord | None:
        row = await self._db.fetchone(
            "SELECT * FROM tool_loop_checkpoints WHERE approval_id = :aid",
            {"aid": approval_id},
        )
        if row is None:
            return None
        return self._row_to_record(row)

    async def delete(self, checkpoint_id: str) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM tool_loop_checkpoints WHERE checkpoint_id = :cid",
            {"cid": checkpoint_id},
        )
        changes = await self._db.changes()
        return changes > 0

    async def cleanup_expired(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            "DELETE FROM tool_loop_checkpoints WHERE expires_at IS NOT NULL AND expires_at < :now",
            {"now": now},
        )
        return await self._db.changes()

    # ── Internal ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: dict) -> LoopCheckpointRecord:
        return LoopCheckpointRecord(
            checkpoint_id=row["checkpoint_id"],
            turn_id=row["turn_id"],
            approval_id=row["approval_id"],
            serialised_state=row["serialised_state"].encode("utf-8") if isinstance(row["serialised_state"], str) else row["serialised_state"],
            integrity_hash=row["integrity_hash"],
            created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
            expires_at=datetime.fromisoformat(row["expires_at"]) if row.get("expires_at") and isinstance(row["expires_at"], str) else None,
        )
