# cogito/infrastructure/tools/audit_repository.py
#
# SQLiteToolAuditRepository — append-only tool execution audit log.
#
# Design rules (see tool-system-spec §16.3):
#   - Audit records are append-only — never UPDATE or DELETE.
#   - Raw arguments are never stored; only hash + redacted summary.
#   - External writes record target, approval ID, and result.

from __future__ import annotations

import json
import logging
from datetime import datetime

from cogito.agent.ports.tools.audit import ToolAuditPort, ToolAuditRecord
from cogito.database.connection import AsyncDatabase

logger = logging.getLogger(__name__)


CREATE_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS tool_execution_audit (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id            TEXT NOT NULL,
    turn_id            TEXT NOT NULL,
    tool_name          TEXT NOT NULL,
    actor_id           TEXT NOT NULL,
    session_id         TEXT,
    status             TEXT NOT NULL,
    risk               TEXT NOT NULL DEFAULT 'read_only',
    started_at         TEXT NOT NULL,
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    arguments_hash     TEXT,
    arguments_redacted TEXT,
    approval_id        TEXT,
    error_code         TEXT,
    policy_reason_code TEXT,
    artifact_ids_json  TEXT DEFAULT '[]',
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;
"""

AUDIT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_audit_turn ON tool_execution_audit(turn_id, call_id);",
    "CREATE INDEX IF NOT EXISTS idx_audit_session ON tool_execution_audit(session_id, started_at);",
    "CREATE INDEX IF NOT EXISTS idx_audit_tool ON tool_execution_audit(tool_name, status, started_at);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_turn_call ON tool_execution_audit(turn_id, call_id);",
]


class SQLiteToolAuditRepository:
    """Append-only audit trail for tool executions."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def ensure_schema(self) -> None:
        await self._db.executescript(CREATE_AUDIT_TABLE)
        for idx in AUDIT_INDEXES:
            try:
                await self._db.execute(idx)
            except Exception:
                pass

    async def record(self, record: ToolAuditRecord) -> None:
        await self._db.execute(
            """INSERT OR IGNORE INTO tool_execution_audit
               (call_id, turn_id, tool_name, actor_id, session_id,
                status, risk, started_at, duration_ms,
                arguments_hash, approval_id, error_code,
                policy_reason_code, artifact_ids_json)
               VALUES (:call_id, :turn_id, :tool_name, :actor_id, :session_id,
                       :status, :risk, :started_at, :duration_ms,
                       :args_hash, :approval_id, :error_code,
                       :policy_code, :artifact_ids)""",
            {
                "call_id": record.call_id,
                "turn_id": record.turn_id,
                "tool_name": record.tool_name,
                "actor_id": record.actor_id,
                "session_id": record.session_id,
                "status": record.status,
                "risk": record.risk,
                "started_at": record.started_at.isoformat() if isinstance(record.started_at, datetime) else record.started_at,
                "duration_ms": record.duration_ms,
                "args_hash": record.arguments_hash,
                "approval_id": record.approval_id,
                "error_code": record.error_code,
                "policy_code": record.policy_reason_code,
                "artifact_ids": json.dumps(list(record.artifact_ids)),
            },
        )

    async def record_batch(self, records: tuple[ToolAuditRecord, ...]) -> None:
        for record in records:
            await self.record(record)
