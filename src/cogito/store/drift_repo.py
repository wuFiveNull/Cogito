"""Drift run / skill-state 持久化。

drift_runs.status 是查询投影，必须由同一事务或 Event Consumer 更新。
tasks/task_attempts 是生命周期权威 —— 本仓库不复制 Task 状态。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from cogito.domain.drift import (
    DriftRunStatus,
    DriftSkillManifest,
)


class DriftRunRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, *, task_id: str, principal_id: str, skill_name: str,
               skill_version: str, admission_snapshot: dict[str, Any],
               status: str = "admitted") -> str:
        now = int(time.time() * 1000)
        run_id = f"dr-{uuid.uuid4().hex[:16]}"
        self._conn.execute(
            "INSERT INTO drift_runs "
            "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
            " status, admission_snapshot_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, task_id, principal_id, skill_name, skill_version,
             status, json.dumps(admission_snapshot, ensure_ascii=False), now),
        )
        self._conn.commit()
        return run_id

    def update_status(self, drift_run_id: str, status: str, **fields: Any) -> None:
        """原子更新 status + 可选字段 (finish_summary, finished_at, candidate_id,...)。"""
        now = int(time.time() * 1000)
        sets = ["status=?", "finished_at=COALESCE(finished_at, ?)"]
        vals: list[Any] = [status, now]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(drift_run_id)
        self._conn.execute(
            f"UPDATE drift_runs SET {', '.join(sets)} WHERE drift_run_id=?",
            vals,
        )
        self._conn.commit()

    def get(self, drift_run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM drift_runs WHERE drift_run_id=?", (drift_run_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def has_active_run(self, principal_id: str) -> bool:
        """同 Principal 是否已有 active Drift (status admitted/running/waiting/paused)。"""
        row = self._conn.execute(
            "SELECT 1 FROM drift_runs WHERE principal_id=? AND status IN "
            "('admitted','running','waiting','paused') LIMIT 1",
            (principal_id,),
        ).fetchone()
        return row is not None


class DriftSkillStateRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, principal_id: str, skill_name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM drift_skill_state WHERE principal_id=? AND skill_name=?",
            (principal_id, skill_name),
        ).fetchone()
        return dict(row) if row else None

    def upsert(self, principal_id: str, skill_name: str, skill_version: str,
               **fields: Any) -> None:
        now = int(time.time() * 1000)
        existing = self.get(principal_id, skill_name)
        if existing is None:
            self._conn.execute(
                "INSERT INTO drift_skill_state "
                "(principal_id, skill_name, skill_version, last_status, "
                " last_run_at, run_count, cursor_json, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (principal_id, skill_name, skill_version,
                 fields.get("last_status"), fields.get("last_run_at"),
                 fields.get("run_count", 1),
                 json.dumps(fields.get("cursor", {}), ensure_ascii=False), now),
            )
            self._conn.commit()
            return
        # 更新
        sets = ["skill_version=?", "updated_at=?"]
        vals: list[Any] = [skill_version, now]
        for k, v in fields.items():
            if k == "run_count":
                sets.append("run_count=run_count+?")
                vals.append(v)
            elif k == "cursor":
                sets.append("cursor_json=?")
                vals.append(json.dumps(v, ensure_ascii=False))
            else:
                sets.append(f"{k}=?")
                vals.append(v)
        vals.extend([principal_id, skill_name])
        self._conn.execute(
            f"UPDATE drift_skill_state SET {', '.join(sets)} "
            "WHERE principal_id=? AND skill_name=?",
            vals,
        )
        self._conn.commit()

    def all_states(self, principal_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM drift_skill_state WHERE principal_id=?",
            (principal_id,),
        ).fetchall()
        return [dict(r) for r in rows]
