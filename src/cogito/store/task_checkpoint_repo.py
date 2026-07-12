"""TaskCheckpointRepository —— Drift 检查点版本化持久化 (PLAN-17 R3 P0-03/04)。

写入顺序（由 write_checkpoint 保证）：
1. JSON 主体落盘（内联到 task_checkpoints.payload_json）；
2. 同步更新 tasks.checkpoint_ref / task_attempts.checkpoint_ref /
   drift_skill_state.checkpoint_ref 指向最新引用；
3. 整批在同一事务 commit。

带真实 task_attempt_id；缺失 / hash 不兼容 → 调用方负责路由到 needs_review。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class TaskCheckpoint:
    checkpoint_id: str
    task_id: str
    task_attempt_id: str
    drift_run_id: str | None
    checkpoint_type: str          # 'drift-step' | 'drift-pause' | 'drift-finish'
    schema_version: int
    payload_ref: str
    payload_json: str
    payload_hash: str
    config_version_id: str | None
    capability_snapshot_version: str | None
    created_at: int

    def verify_hash(self) -> bool:
        return hashlib.sha256(
            self.payload_json.encode("utf-8")).hexdigest() == self.payload_hash


class TaskCheckpointRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, ck: TaskCheckpoint) -> TaskCheckpoint:
        self._conn.execute(
            "INSERT INTO task_checkpoints ("
            "  checkpoint_id, task_id, task_attempt_id, drift_run_id, "
            "  checkpoint_type, schema_version, payload_ref, payload_json, "
            "  payload_hash, config_version_id, capability_snapshot_version, "
            "  created_at"
            ") VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?)",
            (
                ck.checkpoint_id, ck.task_id, ck.task_attempt_id,
                ck.drift_run_id, ck.checkpoint_type, ck.schema_version,
                ck.payload_ref, ck.payload_json, ck.payload_hash,
                ck.config_version_id, ck.capability_snapshot_version,
                ck.created_at,
            ),
        )
        return ck

    def latest_for_task(self, task_id: str) -> TaskCheckpoint | None:
        row = self._conn.execute(
            "SELECT * FROM task_checkpoints WHERE task_id=? "
            "ORDER BY created_at DESC LIMIT 1", (task_id,),
        ).fetchone()
        return self._row_to_ck(row) if row else None

    def latest_for_attempt(self, attempt_id: str) -> TaskCheckpoint | None:
        row = self._conn.execute(
            "SELECT * FROM task_checkpoints WHERE task_attempt_id=? "
            "ORDER BY created_at DESC LIMIT 1", (attempt_id,),
        ).fetchone()
        return self._row_to_ck(row) if row else None

    def list_for_run(self, drift_run_id: str) -> list[TaskCheckpoint]:
        rows = self._conn.execute(
            "SELECT * FROM task_checkpoints WHERE drift_run_id=? "
            "ORDER BY created_at ASC", (drift_run_id,),
        ).fetchall()
        return [self._row_to_ck(r) for r in rows]

    @staticmethod
    def _row_to_ck(row: sqlite3.Row) -> TaskCheckpoint:
        return TaskCheckpoint(
            checkpoint_id=row["checkpoint_id"],
            task_id=row["task_id"],
            task_attempt_id=row["task_attempt_id"],
            drift_run_id=row["drift_run_id"],
            checkpoint_type=row["checkpoint_type"],
            schema_version=row["schema_version"],
            payload_ref=row["payload_ref"],
            payload_json=row["payload_json"],
            payload_hash=row["payload_hash"],
            config_version_id=row["config_version_id"],
            capability_snapshot_version=row["capability_snapshot_version"],
            created_at=row["created_at"],
        )


def _hash_json(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
