"""Command 审计写入 —— interaction-web 的命令统一落地审计。

ACCESS-DELIVERY §2.3：所有命令必须写入 audit_records。
只允许此处 (command 链路) 直接写入审计表；handler 不直写。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any


def write_audit(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    changes: dict[str, Any] | None = None,
    trace_id: str = "",
    commit: bool = True,
) -> str:
    """写入一条审计记录（PLAN-16 M2 TX-06）。

    Args:
        commit: 默认 True，提交当前事务（兼容现有调用方）。
               当审计需要与事实变更在同一事务原子提交时，传入 False，
               由调用方统一 commit，避免"事实先提交、审计失败不回滚"。
    """
    audit_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO audit_records "
        "(audit_id, actor_id, action, target_type, target_id, changes, trace_id, occurred_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            audit_id,
            actor_id,
            action,
            target_type,
            target_id,
            str(changes or {}),
            trace_id,
            datetime.now(UTC).isoformat(),
        ),
    )
    if commit:
        conn.commit()
    return audit_id
