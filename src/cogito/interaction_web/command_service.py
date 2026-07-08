"""CommandService —— 命令可写转写的薄服务层。

涵盖现有领域服务未覆盖的状态转写：审批 (approvals)、投递重放 (deliveries)。
handler 调用此处方法，不直接执行 SQL —— 遵守"增删改查一律走服务"。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def set_approval_decision(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
    decision: str,  # 'approved' | 'rejected'
    responder_id: str,
) -> bool:
    """写入审批决定。返回 False 表示审批不存在或非 pending。"""
    row = conn.execute(
        "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
    ).fetchone()
    if row is None or row["status"] != "pending":
        return False
    conn.execute(
        "UPDATE approvals SET status=?, responder_id=?, "
        "decided_at=? WHERE approval_id=?",
        (decision, responder_id, datetime.now(UTC).isoformat(), approval_id),
    )
    conn.commit()
    return True


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
