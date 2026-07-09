"""PR-C3: SideEffect reconcile — Plan 03 M3."""
from __future__ import annotations

from cogito.capability.models import SideEffectReceipt
from cogito.service.reconcile_service import ReconcileService


class _DB:
    """Minimal in-memory store for receipt updates + approval creation."""

    def __init__(self) -> None:
        self.receipts: dict[str, dict] = {}
        self.approvals: list[dict] = []

    def execute(self, sql: str, params: tuple = ()) -> "_Cursor":
        if sql.startswith("UPDATE"):
            rid = params[-1]
            self.receipts[rid] = {"reconcile_status": params[0], "summary": params[1]}
            return _Cursor(1)
        if sql.startswith("INSERT INTO approvals"):
            self.approvals.append({"approval_id": params[0], "request": params[2]})
            return _Cursor(1)
        return _Cursor(0)

    def commit(self) -> None:
        pass


class _Cursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


def _receipt(rid: str = "r1", op_id: str = "op-123",
             req_hash: str = "abc") -> SideEffectReceipt:
    return SideEffectReceipt(
        receipt_id=rid, tool_call_id="c1",
        external_operation_id=op_id, request_hash=req_hash,
        status="unknown",
    )


def test_reconcile_queried_first() -> None:
    """有 operation_id + query_fn → 用 operation_id 查询平台。"""
    svc = ReconcileService(_DB())
    action = svc.reconcile(
        _receipt(),
        platform_query_fn=lambda op_id: "succeeded",
    )
    assert action.action_taken == "queried"
    assert action.success is True


def test_reconcile_idempotent_replay_when_no_op_id() -> None:
    """无 operation_id → idempotent replay。"""
    svc = ReconcileService(_DB())
    action = svc.reconcile(
        _receipt(op_id=""),
        idempotent_replay_fn=lambda h: "ok",
    )
    assert action.action_taken == "idempotent_replay"
    assert action.success is True


def test_reconcile_manual_approval_fallback() -> None:
    """无任何信息 → 创建人工审批。"""
    db = _DB()
    svc = ReconcileService(db)
    action = svc.reconcile(_receipt(op_id="", req_hash=""))
    assert action.action_taken == "manual_approval"
    assert action.success is True
    assert len(db.approvals) == 1


def test_reconcile_not_blindly_retried() -> None:
    """禁止盲目重试：reconcile 不自动重试 side_effect。"""
    svc = ReconcileService(_DB())
    action = svc.reconcile(_receipt("", ""))
    # 无 operation_id + 无 request_hash → manual approval, NOT retry
    assert action.action_taken == "manual_approval"
    assert "retry" not in action.detail.lower()
