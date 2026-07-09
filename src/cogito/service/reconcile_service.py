"""ReconcileService —— unknown 副作用的对账 (Plan 03 M3).

当 RecoveryDecision=reconcile 时，只允许：
1. 用 external_operation_id 查询平台
2. 使用相同 idempotency_key 请求平台（幂等重放）
3. 创建人工处理 Approval

禁止：盲目重试产生重复副作用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cogito.capability.models import SideEffectReceipt


@dataclass(frozen=True)
class ReconcileAction:
    """对账操作结果。"""
    receipt_id: str
    action_taken: str  # "queried" | "idempotent_replay" | "manual_approval" | "no_op"
    success: bool
    detail: str


class ReconcileService:
    """对账服务：处理 unknown 副作用。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def reconcile(
        self,
        receipt: SideEffectReceipt,
        *,
        platform_query_fn: Any = None,      # callable: operation_id -> status
        idempotent_replay_fn: Any = None,    # callable: request_hash -> result
    ) -> ReconcileAction:
        """对账一个 unknown receipt（三选一）。"""
        # 1. 尝试用 external_operation_id 查询平台
        if receipt.external_operation_id and platform_query_fn:
            try:
                status = platform_query_fn(receipt.external_operation_id)
                self._update_receipt(receipt.receipt_id, "reconciled",
                                     f"Queried platform: {status}")
                return ReconcileAction(
                    receipt_id=receipt.receipt_id,
                    action_taken="queried",
                    success=True,
                    detail=f"Platform status: {status}",
                )
            except Exception:
                pass

        # 2. 使用相同 idempotency key 请求平台（幂等重放）
        if receipt.request_hash and idempotent_replay_fn:
            try:
                result = idempotent_replay_fn(receipt.request_hash)
                self._update_receipt(receipt.receipt_id, "reconciled",
                                     f"Idempotent replay: {result}")
                return ReconcileAction(
                    receipt_id=receipt.receipt_id,
                    action_taken="idempotent_replay",
                    success=True,
                    detail=f"Replay result: {result}",
                )
            except Exception:
                pass

        # 3. 无法自动确认 → 创建人工处理 Approval
        approval_id = self._create_manual_approval(receipt)
        self._update_receipt(receipt.receipt_id, "manual",
                             f"Escalated to approval {approval_id}")
        return ReconcileAction(
            receipt_id=receipt.receipt_id,
            action_taken="manual_approval",
            success=True,
            detail=f"Manual approval created: {approval_id}",
        )

    def _update_receipt(self, receipt_id: str, reconcile_status: str,
                        summary: str) -> None:
        try:
            self._conn.execute(
                "UPDATE side_effect_receipts SET reconcile_status=?, summary=? "
                "WHERE receipt_id=?",
                (reconcile_status, summary, receipt_id),
            )
            self._conn.commit()
        except Exception:
            pass

    def _create_manual_approval(self, receipt: SideEffectReceipt) -> str:
        import uuid
        from datetime import UTC, datetime, timedelta

        approval_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        try:
            self._conn.execute(
                "INSERT INTO approvals "
                "(approval_id, turn_id, request, status, expires_at, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (
                    approval_id,
                    "",
                    f'{{"reconcile_receipt_id": "{receipt.receipt_id}", '
                    f'"tool_call_id": "{receipt.tool_call_id}"}}',
                    (now + timedelta(hours=24)).isoformat(),
                    now.isoformat(),
                ),
            )
            self._conn.commit()
        except Exception:
            pass
        return approval_id
