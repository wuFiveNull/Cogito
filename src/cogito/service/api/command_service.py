"""CommandService —— 命令可写转写的薄服务层。

涵盖现有领域服务未覆盖的状态转写：审批 (approvals)、投递重放 (deliveries)。
handler 调用此处方法，不直接执行 SQL —— 遵守"增删改查一律走服务"。
"""

from __future__ import annotations

import sqlite3
import json
from datetime import UTC, datetime


def set_approval_decision(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
    decision: str,  # 'approved' | 'rejected'
    responder_id: str,
    expected_version: int | None = None,
    action_hash: str = "",
    response_reason: str = "",
) -> bool:
    """写入审批决定。返回 False 表示审批不存在或非 pending。"""
    row = conn.execute(
        "SELECT status,expires_at,version,action_hash,allowed_responder_principal_ids "
        "FROM approvals WHERE approval_id=?", (approval_id,)
    ).fetchone()
    if row is None or row["status"] != "pending":
        return False
    now = datetime.now(UTC)
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= now:
        return False
    if expected_version is not None and int(row["version"]) != expected_version:
        return False
    if action_hash and row["action_hash"] != action_hash:
        return False
    allowed = set(json.loads(row["allowed_responder_principal_ids"] or "[]"))
    if allowed and responder_id not in allowed:
        return False
    cur = conn.execute(
        "UPDATE approvals SET status=?, responder_id=?, decided_at=?, responded_at=?, "
        "response_reason=?, version=version+1 WHERE approval_id=? "
        "AND status='pending' AND version=? AND expires_at>?",
        (decision, responder_id, now.isoformat(), now.isoformat(), response_reason,
         approval_id, row["version"], now.isoformat()),
    )
    return cur.rowcount > 0


def replay_delivery(conn: sqlite3.Connection, *, delivery_id: str) -> bool:
    """把 failed/cancelled 的投递重新置为 pending。False 表示不存在或状态不可重放。"""
    row = conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", (delivery_id,)
    ).fetchone()
    if row is None or row["status"] not in ("failed", "cancelled"):
        return False
    conn.execute(
        "UPDATE deliveries SET status='pending', last_error=NULL "
        "WHERE delivery_id=?",
        (delivery_id,),
    )
    conn.commit()
    return True


def resume_turn_after_approval(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
) -> str | None:
    """审批消费后把关联 Turn 从 waiting_user → queued（仅一次）。

    返回 turn_id 表示成功；None 表示 approval 不存在/非 pending/无关联 Turn。
    幂等：重复消费同一 approved approval 不产生第二个 queued 状态。
    """
    row = conn.execute(
        "SELECT turn_id, status FROM approvals WHERE approval_id=?",
        (approval_id,),
    ).fetchone()
    if row is None or row["status"] != "approved":
        return None
    turn_id = row["turn_id"]
    if not turn_id:
        return None
    # 仅 waiting_user/waiting_external 可恢复；已 queued 说明已被消费过（幂等）
    cur = conn.execute(
        "UPDATE turns SET status='queued', version=version+1 "
        "WHERE turn_id=? AND status IN ('waiting_user','waiting_external')",
        (turn_id,),
    )
    if cur.rowcount == 0:
        return None  # 已消费或状态不对
    # Child-Agent approvals resume their durable Task as well as the child Turn.
    conn.execute(
        "UPDATE tasks SET status='queued',checkpoint_ref=NULL "
        "WHERE task_id IN (SELECT task_id FROM child_task_links WHERE turn_id=?) "
        "AND status='waiting_user'",
        (turn_id,),
    )
    return turn_id
