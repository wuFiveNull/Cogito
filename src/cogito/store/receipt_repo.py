"""SideEffectReceiptRepository —— side_effect_receipts 表数据访问（Plan 03 M3）。

记录每个 Tool 执行的外部操作 ID、请求哈希、状态与对账结果。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class ReceiptRecord:
    receipt_id: str
    capability_id: str
    operation_id: str | None
    request_hash: str
    side_effect_class: str
    status: str
    reconcile_status: str = "not_needed"
    raw_ref: str | None = None
    summary: str | None = None
    attempt_id: str = ""
    attempt_type: str = "run"
    created_at: int = 0
    resolved_at: int | None = None
    audit_id: str | None = None


class SideEffectReceiptRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ReceiptRecord) -> None:
        self._conn.execute(
            "INSERT INTO side_effect_receipts (receipt_id, capability_id, operation_id, "
            "request_hash, side_effect_class, status, reconcile_status, raw_ref, summary, "
            "attempt_id, attempt_type, created_at, audit_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.receipt_id, record.capability_id, record.operation_id,
             record.request_hash, record.side_effect_class, record.status,
             record.reconcile_status, record.raw_ref, record.summary,
             record.attempt_id, record.attempt_type, record.created_at, record.audit_id),
        )

    def get(self, receipt_id: str) -> ReceiptRecord | None:
        row = self._conn.execute(
            "SELECT * FROM side_effect_receipts WHERE receipt_id=?", (receipt_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_attempt(self, attempt_type: str, attempt_id: str) -> list[ReceiptRecord]:
        rows = self._conn.execute(
            "SELECT * FROM side_effect_receipts WHERE attempt_type=? AND attempt_id=? "
            "ORDER BY created_at ASC",
            (attempt_type, attempt_id),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_pending_reconcile(self, limit: int = 50) -> list[ReceiptRecord]:
        """查询需要人工/自动对账的 unknown 收据。"""
        rows = self._conn.execute(
            "SELECT * FROM side_effect_receipts "
            "WHERE status='unknown' AND reconcile_status='pending' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def update_status(self, receipt_id: str, status: str, resolved_at: int | None = None) -> None:
        self._conn.execute(
            "UPDATE side_effect_receipts SET status=?, resolved_at=? WHERE receipt_id=?",
            (status, resolved_at, receipt_id),
        )

    def update_reconcile(
        self, receipt_id: str, reconcile_status: str,
        summary: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE side_effect_receipts SET reconcile_status=?, summary=? WHERE receipt_id=?",
            (reconcile_status, summary, receipt_id),
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ReceiptRecord:
        return ReceiptRecord(
            receipt_id=row["receipt_id"],
            capability_id=row["capability_id"],
            operation_id=row["operation_id"],
            request_hash=row["request_hash"],
            side_effect_class=row["side_effect_class"],
            status=row["status"],
            reconcile_status=row["reconcile_status"],
            raw_ref=row["raw_ref"],
            summary=row["summary"],
            attempt_id=row["attempt_id"],
            attempt_type=row["attempt_type"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            audit_id=row["audit_id"],
        )
