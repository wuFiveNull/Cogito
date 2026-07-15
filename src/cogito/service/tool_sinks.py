"""Sink adapters — bridge concrete store repos to capability Port contracts.

The adapter lives in service composition so the capability layer remains
independent from concrete repositories.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from cogito.store.receipt_repo import ReceiptRecord, SideEffectReceiptRepository
from cogito.store.tool_call_repo import ToolCallRecord, ToolCallRepository


class ToolCallRepositorySink:
    """ToolCallSink 的 ToolCallRepository 薄包装。

    executor._persist_start 传入完整记录（insert）；
    executor._persist_end 传入精简记录（update_status）。
    通过 record 里是否含 started_at 区分语义。
    """

    def __init__(
        self,
        repo: ToolCallRepository,
        receipt_repo: SideEffectReceiptRepository | None = None,
        task_service: object | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._repo = repo
        self._receipt_repo = receipt_repo
        self._task_service = task_service
        self._connection = connection

    def insert(self, record: object) -> None:
        rec = record if isinstance(record, dict) else {}
        if "started_at" in rec:
            # 完整记录 → insert
            self._repo.insert(
                ToolCallRecord(
                    tool_call_id=rec.get("tool_call_id", ""),
                    attempt_id=rec.get("attempt_id", ""),
                    attempt_type=rec.get("attempt_type", "run"),
                    tool_name=rec.get("tool_name", ""),
                    tool_version=rec.get("tool_version", "1.0"),
                    arguments=rec.get("arguments", "{}"),
                    arguments_ref=rec.get("arguments_ref", ""),
                    idempotency_key=rec.get("idempotency_key", ""),
                    status=rec.get("status", "pending"),
                    started_at=rec.get("started_at"),
                    completed_at=rec.get("completed_at"),
                    constraints_json=rec.get("constraints_json", "{}"),
                )
            )
        else:
            # 精简记录 → update_status
            self._repo.update_status(
                rec.get("tool_call_id", ""),
                rec.get("status", ""),
                completed_at=rec.get("completed_at"),
                result_ref=rec.get("result_ref", ""),
                result_summary=rec.get("result_summary", ""),
                result_trust_label=rec.get("result_trust_label", "unverified"),
                result_size_bytes=rec.get("result_size_bytes", 0),
            )

    def insert_receipt(self, record: dict) -> str:
        if self._receipt_repo is None:
            return ""
        receipt_id = uuid.uuid4().hex
        self._receipt_repo.insert(
            ReceiptRecord(
                receipt_id=receipt_id,
                capability_id=record.get("capability_id", ""),
                operation_id=record.get("operation_id"),
                request_hash=record.get("request_hash", ""),
                side_effect_class=record.get("side_effect_class", "reconcilable"),
                status=record.get("status", "succeeded"),
                reconcile_status=record.get("reconcile_status", "not_needed"),
                summary=record.get("summary", "")[:2_000],
                attempt_id=record.get("attempt_id", ""),
                created_at=record.get("created_at", 0),
            )
        )
        return receipt_id

    def enqueue_reconcile(self, record: dict) -> str:
        """Queue reconciliation without ever replaying the original operation."""
        if self._task_service is None:
            return ""
        receipt_id = str(record.get("receipt_id", ""))
        task = self._task_service.create(
            "tool.reconcile",
            json.dumps(record, ensure_ascii=False, sort_keys=True),
            idempotency_key=f"tool.reconcile:{receipt_id or record.get('tool_call_id', '')}",
            origin="tool_executor",
            retry_policy={"max_attempts": 1},
        )
        return str(getattr(task, "task_id", ""))

    def claim_deferred_result(self, turn_id: str) -> dict | None:
        if self._connection is None:
            return None
        row = self._connection.execute(
            "SELECT w.waiting_id,w.subject_id,w.payload_json,d.parent_tool_call_id,d.result_ref "
            "FROM waiting_conditions w JOIN agent_delegations d ON d.delegation_id=w.subject_id "
            "WHERE w.owner_type='turn' AND w.owner_id=? AND w.condition_type='child_join' "
            "AND w.status='satisfied' AND w.consumed_at IS NULL ORDER BY w.satisfied_at LIMIT 1",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        consumed = self._connection.execute(
            "UPDATE waiting_conditions SET consumed_at=?,version=version+1 "
            "WHERE waiting_id=? AND consumed_at IS NULL AND status='satisfied'",
            (datetime.now(UTC).isoformat(), row["waiting_id"]),
        )
        if consumed.rowcount != 1:
            self._connection.rollback()
            return None
        self._connection.commit()
        return {
            "waiting_id": row["waiting_id"],
            "tool_call_id": row["parent_tool_call_id"],
            "tool_name": "delegate_task",
            "result": row["result_ref"],
        }
