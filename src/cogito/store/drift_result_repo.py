"""DriftResultRepository —— Drift 完成结果持久化 (PLAN-17 R5 P0-06)。

在 Drift Handler 完成事务中写 DriftResult + Outbox DriftResultCommitted;
Consumer 校验后调 DriftProjectionService, 成功后回写 candidate_id/emitted。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class DriftResult:
    drift_result_id: str
    drift_run_id: str
    task_attempt_id: str
    result_kind: str                   # 'internal_only' | 'candidate_emission' | 'skipped_no_value'
    result_ref: str
    summary: str = ""
    items: list[dict[str, Any]] | None = None
    candidate_draft: dict[str, Any] | None = None
    candidate_id: str | None = None
    emitted: bool = False
    created_at: int = 0


class DriftResultRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, result: DriftResult) -> DriftResult:
        self._conn.execute(
            "INSERT INTO drift_results ("
            "  drift_result_id, drift_run_id, task_attempt_id, result_kind, "
            "  result_ref, summary, items_json, candidate_draft_json, "
            "  candidate_id, emitted, created_at"
            ") VALUES (?,?,?,?,?, ?,?,?,?, ?,?)",
            (
                result.drift_result_id, result.drift_run_id,
                result.task_attempt_id, result.result_kind, result.result_ref,
                result.summary,
                json.dumps(result.items or [], ensure_ascii=False),
                json.dumps(result.candidate_draft, ensure_ascii=False)
                if result.candidate_draft else None,
                result.candidate_id, 1 if result.emitted else 0,
                result.created_at,
            ),
        )
        return result

    def mark_emitted(self, drift_result_id: str, candidate_id: str) -> None:
        self._conn.execute(
            "UPDATE drift_results SET candidate_id=?, emitted=1 "
            "WHERE drift_result_id=?",
            (candidate_id, drift_result_id),
        )
        self._conn.commit()

    def get(self, drift_result_id: str) -> DriftResult | None:
        row = self._conn.execute(
            "SELECT * FROM drift_results WHERE drift_result_id=?",
            (drift_result_id,),
        ).fetchone()
        return self._row_to_result(row) if row else None

    def latest_for_run(self, drift_run_id: str) -> DriftResult | None:
        row = self._conn.execute(
            "SELECT * FROM drift_results WHERE drift_run_id=? "
            "ORDER BY created_at DESC LIMIT 1", (drift_run_id,),
        ).fetchone()
        return self._row_to_result(row) if row else None

    def _row_to_result(self, row: sqlite3.Row) -> DriftResult:
        items_raw = row["items_json"] or "[]"
        try:
            items = json.loads(items_raw)
        except Exception:
            items = []
        draft_raw = row["candidate_draft_json"]
        try:
            draft = json.loads(draft_raw) if draft_raw else None
        except Exception:
            draft = None
        return DriftResult(
            drift_result_id=row["drift_result_id"],
            drift_run_id=row["drift_run_id"],
            task_attempt_id=row["task_attempt_id"],
            result_kind=row["result_kind"],
            result_ref=row["result_ref"],
            summary=row["summary"] or "",
            items=items,
            candidate_draft=draft,
            candidate_id=row["candidate_id"],
            emitted=bool(row["emitted"]),
            created_at=row["created_at"],
        )
