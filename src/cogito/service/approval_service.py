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

    def approve(
        self,
        approval_id: str,
        responder_id: str,
        *,
        expected_version: int | None = None,
        action_hash: str = "",
    ) -> ApprovalDecision:
        """批准。返回新状态；非法转换由异常表达。"""
        ...

    def reject(
        self,
        approval_id: str,
        responder_id: str,
        *,
        expected_version: int | None = None,
    ) -> ApprovalDecision:
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
        allowed = sorted({"owner", str(request.get("principal_id", "owner"))})
        columns = self._columns()
        if "subject_type" not in columns:
            self._conn.execute(
                "INSERT INTO approvals(approval_id,turn_id,request,status,expires_at,created_at) "
                "VALUES (?,?,?,'pending',?,?)",
                (
                    approval_id,
                    turn_id,
                    json.dumps(request, ensure_ascii=False),
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
            self._conn.commit()
            return ApprovalRequest(approval_id, turn_id, request, expires_at)
        self._conn.execute(
            "INSERT INTO approvals "
            "(approval_id, turn_id, request, status, expires_at, created_at, "
            "subject_type,subject_id,requester_attempt_id,capability_id,capability_version,"
            "arguments_snapshot_ref,action_hash,requested_permissions,risk_level,"
            "policy_version,auto_mode_version,constraints_json,"
            "allowed_responder_principal_ids,version) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                approval_id,
                turn_id,
                json.dumps(request, ensure_ascii=False),
                expires_at.isoformat(),
                now.isoformat(),
                "tool_call",
                str(request.get("tool_call_id", "")),
                str(request.get("attempt_id", "")),
                str(request.get("capability_id", "")),
                str(request.get("tool_version", "")),
                str(request.get("arguments_snapshot_ref", "")),
                str(request.get("arguments_hash", "")),
                json.dumps(request.get("permissions", []), ensure_ascii=False),
                str(request.get("risk_level", "low")),
                str(request.get("policy_version", "")),
                str(request.get("auto_mode_version", "")),
                json.dumps(request.get("constraints", {}), ensure_ascii=False),
                json.dumps(allowed, ensure_ascii=False),
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
        self,
        approval_id: str,
        responder_id: str,
        decision: str,
        *,
        expected_version: int | None = None,
        action_hash: str = "",
    ) -> ApprovalDecision:
        now = datetime.now(UTC)
        columns = self._columns()
        if "version" not in columns:
            row = self._conn.execute(
                "SELECT status,expires_at FROM approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalNotFoundError(approval_id)
            if row["status"] != "pending":
                raise ApprovalStateError(f"approval {approval_id} already {row['status']}")
            if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < now:
                raise ApprovalStateError(f"approval {approval_id} expired")
            cur = self._conn.execute(
                "UPDATE approvals SET status=?,responder_id=?,decided_at=? "
                "WHERE approval_id=? AND status='pending'",
                (decision, responder_id, now.isoformat(), approval_id),
            )
            if not cur.rowcount:
                raise ApprovalStateError("approval was concurrently decided")
            self._conn.commit()
            return ApprovalDecision(approval_id, decision, responder_id, now)
        row = self._conn.execute(
            "SELECT status, expires_at, version, action_hash, "
            "allowed_responder_principal_ids FROM approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(approval_id)
        if row["status"] != "pending":
            raise ApprovalStateError(f"approval {approval_id} already {row['status']}")
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < now:
            raise ApprovalStateError(f"approval {approval_id} expired")
        import json

        allowed = set(json.loads(row["allowed_responder_principal_ids"] or "[]"))
        if allowed and responder_id not in allowed:
            raise ApprovalStateError("responder principal is not allowed")
        if expected_version is not None and int(row["version"]) != expected_version:
            raise ApprovalStateError("approval version conflict")
        if action_hash and row["action_hash"] != action_hash:
            raise ApprovalStateError("approval action hash mismatch")
        cur = self._conn.execute(
            "UPDATE approvals SET status=?, responder_id=?, decided_at=?, responded_at=?, "
            "version=version+1 WHERE approval_id=? AND status='pending' AND version=?",
            (decision, responder_id, now.isoformat(), now.isoformat(), approval_id, row["version"]),
        )
        if not cur.rowcount:
            raise ApprovalStateError("approval was concurrently decided")
        self._conn.commit()
        return ApprovalDecision(
            approval_id=approval_id,
            status=decision,
            responder_id=responder_id,
            decided_at=now,
        )

    def approve(
        self,
        approval_id: str,
        responder_id: str,
        *,
        expected_version: int | None = None,
        action_hash: str = "",
    ) -> ApprovalDecision:
        return self._transition(
            approval_id,
            responder_id,
            "approved",
            expected_version=expected_version,
            action_hash=action_hash,
        )

    def reject(
        self,
        approval_id: str,
        responder_id: str,
        *,
        expected_version: int | None = None,
    ) -> ApprovalDecision:
        return self._transition(
            approval_id,
            responder_id,
            "rejected",
            expected_version=expected_version,
        )

    def expire(self, approval_id: str) -> bool:
        version_update = ", version=version+1" if "version" in self._columns() else ""
        cur = self._conn.execute(
            f"UPDATE approvals SET status='expired'{version_update} "
            "WHERE approval_id=? AND status='pending'",
            (approval_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def cancel(self, approval_id: str) -> bool:
        version_update = ", version=version+1" if "version" in self._columns() else ""
        cur = self._conn.execute(
            f"UPDATE approvals SET status='cancelled'{version_update} "
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

    def _columns(self) -> set[str]:
        return {
            str(row["name"] if hasattr(row, "keys") else row[1])
            for row in self._conn.execute("PRAGMA table_info(approvals)").fetchall()
        }

    def find_or_create_tool_approval(
        self,
        *,
        turn_id: str,
        request: dict[str, Any],
        ttl_seconds: int = 3600,
    ) -> ApprovalRequest:
        """Return an equivalent pending approval or create one.

        Equivalence is intentionally strict: capability version and canonical
        argument hash must match.  This prevents repeated model iterations from
        flooding the approval queue while preserving argument binding.
        """
        import json

        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE turn_id=? AND status='pending' ORDER BY created_at DESC",
            (turn_id,),
        ).fetchall()
        for row in rows:
            try:
                existing = json.loads(row["request"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                existing.get("kind") == "tool_call"
                and existing.get("capability_id") == request.get("capability_id")
                and existing.get("tool_version") == request.get("tool_version")
                and existing.get("tool_schema_hash") == request.get("tool_schema_hash")
                and existing.get("arguments_hash") == request.get("arguments_hash")
            ):
                return ApprovalRequest(
                    approval_id=row["approval_id"],
                    turn_id=turn_id,
                    request=existing,
                    expires_at=datetime.fromisoformat(row["expires_at"]),
                )
        return self.create(turn_id=turn_id, request=request, ttl_seconds=ttl_seconds)

    def claim_approved_tool_call(self, turn_id: str) -> dict[str, Any] | None:
        """Read one approved call; consumption happens after runtime revalidation."""
        import json

        rows = self._conn.execute(
            "SELECT approval_id, request, version, consumed_at FROM approvals "
            "WHERE turn_id=? AND status='approved' AND consumed_at IS NULL "
            "ORDER BY decided_at ASC",
            (turn_id,),
        ).fetchall()
        for row in rows:
            try:
                request = json.loads(row["request"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if request.get("kind") != "tool_call" or row["consumed_at"]:
                continue
            request["approval_id"] = row["approval_id"]
            request["approval_version"] = row["version"]
            return request
        return None

    def consume_approved_tool_call(self, approval_id: str, expected_version: int) -> bool:
        """Atomically consume a fully revalidated approved call exactly once."""
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE approvals SET consumed_at=?,version=version+1 "
            "WHERE approval_id=? AND status='approved' AND consumed_at IS NULL "
            "AND version=? AND expires_at>?",
            (now, approval_id, expected_version, now),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def invalidate_approved_tool_call(self, approval_id: str, expected_version: int) -> bool:
        """Invalidate a stale approved call without executing it."""
        cur = self._conn.execute(
            "UPDATE approvals SET status='cancelled',version=version+1 "
            "WHERE approval_id=? AND status='approved' AND consumed_at IS NULL AND version=?",
            (approval_id, expected_version),
        )
        self._conn.commit()
        return cur.rowcount == 1
