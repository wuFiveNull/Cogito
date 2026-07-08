"""ApprovalService —— Approval 聚合的唯一公开写入口。

SYSTEM-BOUNDARIES / 4: Approval 的唯一写入者是 ApprovalService。
入站 Tool 审批、高风险操作授权、等待恢复都经此接口。

当前实现：`SqliteApprovalService`（SQLite 后端）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class ApprovalRequest:
    """审批请求参数。"""
    approval_id: str
    turn_id: str
    request: dict[str, Any]
    expires_at: datetime


@dataclass(frozen=True)
class ApprovalDecision:
    """审批决策结果。"""
    approval_id: str
    status: str  # 'approved' | 'rejected'
    responder_id: str
    decided_at: datetime


class ApprovalService(Protocol):
    """Approval 生命周期管理接口。

    唯一写入口：所有 Approval 的状态变更经此接口。
    """

    def create(
        self,
        *,
        turn_id: str,
        request: dict[str, Any],
        ttl_seconds: int = 3600,
    ) -> ApprovalRequest:
        """创建一个 pending 的 Approval。"""
        ...

    def approve(self, approval_id: str, responder_id: str) -> ApprovalDecision:
        """批准。返回新状态；非法转换由异常表达。"""
        ...

    def reject(self, approval_id: str, responder_id: str) -> ApprovalDecision:
        """拒绝。返回新状态；非法转换由异常表达。"""
        ...

    def expire(self, approval_id: str) -> bool:
        """标记过期（仅 pending 可过期）。"""
        ...

    def cancel(self, approval_id: str) -> bool:
        """取消（仅 pending 可取消）。"""
        ...

    def get(self, approval_id: str) -> dict[str, Any] | None:
        """按 ID 获取 Approval 原始记录。"""
        ...


class ApprovalStateError(ValueError):
    """审批状态非法转换。"""


class ApprovalNotFoundError(KeyError):
    """审批不存在。"""


class SqliteApprovalService:
    """ApprovalService 的 SQLite 实现。"""

    VALID_TERMINAL = {"approved", "rejected", "expired", "cancelled"}

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def create(
        self,
        *,
        turn_id: str,
        request: dict[str, Any],
        ttl_seconds: int = 3600,
    ) -> ApprovalRequest:
        import json
        import uuid
        from datetime import timedelta

        now = datetime.now(UTC)
        approval_id = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=ttl_seconds)
        self._conn.execute(
            "INSERT INTO approvals "
            "(approval_id, turn_id, request, status, expires_at, created_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (
                approval_id,
                turn_id,
                json.dumps(request, ensure_ascii=False),
                expires_at.isoformat(),
                now.isoformat(),
            ),
        )
        self._conn.commit()
        return ApprovalRequest(
            approval_id=approval_id,
            turn_id=turn_id,
            request=request,
            expires_at=expires_at,
        )

    def _transition(
        self, approval_id: str, responder_id: str, decision: str,
    ) -> ApprovalDecision:
        now = datetime.now(UTC)
        row = self._conn.execute(
            "SELECT status, expires_at FROM approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(approval_id)
        if row["status"] != "pending":
            raise ApprovalStateError(
                f"approval {approval_id} already {row['status']}"
            )
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < now:
            raise ApprovalStateError(f"approval {approval_id} expired")
        self._conn.execute(
            "UPDATE approvals SET status=?, responder_id=?, decided_at=? "
            "WHERE approval_id=?",
            (decision, responder_id, now.isoformat(), approval_id),
        )
        self._conn.commit()
        return ApprovalDecision(
            approval_id=approval_id,
            status=decision,
            responder_id=responder_id,
            decided_at=now,
        )

    def approve(self, approval_id: str, responder_id: str) -> ApprovalDecision:
        return self._transition(approval_id, responder_id, "approved")

    def reject(self, approval_id: str, responder_id: str) -> ApprovalDecision:
        return self._transition(approval_id, responder_id, "rejected")

    def expire(self, approval_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE approvals SET status='expired' "
            "WHERE approval_id=? AND status='pending'",
            (approval_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def cancel(self, approval_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE approvals SET status='cancelled' "
            "WHERE approval_id=? AND status='pending'",
            (approval_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return dict(row) if row else None
