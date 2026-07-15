"""DriftAdmissionService —— 全局 idle admission (PROACTIVE-IDLE / 9-11)。

确定性、只读事务快照：读取 Turn/Task/Delivery/Outbox/Recovery/Activity/Budget，
输出 admit | deny + 结构化 reason list + snapshot time。

Admission 不得使用模型；全部基于阈值比较。
"""

from __future__ import annotations

import time
from typing import Any

from cogito.domain.drift import DriftAdmissionSnapshot, DriftReasonCode


class DriftAdmissionResult:
    """admission 结果（deny 带 reason list）。"""

    def __init__(
        self,
        admit: bool,
        reasons: list[str] | None = None,
        snapshot: DriftAdmissionSnapshot | None = None,
    ) -> None:
        self.admit = admit
        self.reasons: list[str] = reasons or []
        self.snapshot = snapshot or DriftAdmissionSnapshot()

    def __repr__(self) -> str:
        if self.admit:
            return "DriftAdmissionResult(admit)"
        return f"DriftAdmissionResult(deny, reasons={self.reasons})"


def admit(
    conn,
    *,
    principal_id: str = "owner",
    idle_after_minutes: int = 30,
    max_runs_per_day: int = 3,
    max_concurrent: int = 1,
    high_priority_backlog_threshold: int = 1,
    outbox_critical_age_ms: int = 5000,
    presence_reader: Any = None,
) -> DriftAdmissionResult:
    """全局 idle 检查。全部阈值满足才 admit。"""
    now_ms = int(time.time() * 1000)
    reasons: list[str] = []

    # 1. active normal turns (running/queued turns)
    row = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE status IN ('running','queued','accepted')"
    ).fetchone()
    active_turns = row[0] if row else 0

    # 2. high-priority task backlog (priority >= 50 且 queued/running)
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running') AND priority >= 50"
    ).fetchone()
    priority_backlog = row[0] if row else 0

    # 3. ready delivery backlog (pending)
    row = conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='pending'").fetchone()
    delivery_backlog = row[0] if row else 0

    # 4. outbox critical age (最老的 pending outbox event)。
    #    准入条件：outbox_critical_age < threshold（无 pending 时视为满足）。
    row = conn.execute(
        "SELECT MIN(created_at) FROM outbox_events WHERE status='pending'"
    ).fetchone()
    oldest_outbox_age_ms: int | None = None
    if row and row[0] is not None:
        oldest_outbox = row[0] if isinstance(row[0], int) else 0
        oldest_outbox_age_ms = max(0, now_ms - oldest_outbox)
    outbox_age_ms = oldest_outbox_age_ms if oldest_outbox_age_ms is not None else 0

    # 5. recovery in progress (近 grace_period 内存在恢复过的 attempt → 视为进行中)
    #    简化：有 in-progress 的 turn/streaming delivery 即为 recovery
    row = conn.execute("SELECT COUNT(*) FROM turns WHERE status='running'").fetchone()
    recovery_in_progress = (row[0] if row else 0) > 0

    # 6. user activity age
    last_user_activity_age_ms = None
    last_user_dt = None
    if presence_reader is not None:
        try:
            last_user_dt = presence_reader.get_last_user_activity(principal_id)
        except Exception:
            last_user_dt = None
    if last_user_dt is not None:
        from cogito.contracts.clock import epoch_ms

        lu = epoch_ms(last_user_dt)
        if lu is not None:
            last_user_activity_age_ms = max(0, now_ms - lu)

    # 7. daily drift budget (当日已完成/进行的 run 数)
    day_start_ms = now_ms - (now_ms % 86400000)  # 粗略日切（UTC）；精确切在 service 层
    row = conn.execute(
        "SELECT COUNT(*) FROM drift_runs WHERE principal_id=? AND created_at>=? "
        "AND status NOT IN ('failed')",
        (principal_id, day_start_ms),
    ).fetchone()
    runs_today = row[0] if row else 0
    daily_budget_remaining = max(0, max_runs_per_day - runs_today)

    # 8. drift active count vs max_concurrent (PLAN-17 R6 DR-P1-02)
    row = conn.execute(
        "SELECT COUNT(*) FROM drift_runs WHERE principal_id=? AND status IN "
        "('admitted','running','waiting','paused')",
        (principal_id,),
    ).fetchone()
    active_drift_count = int(row[0]) if row else 0
    # partial unique index (uq_drift_one_active_per_principal) 保证 status='admitted/
    # running/waiting/paused' 行 per principal 唯一 → active_drift_count 实际最大为 1。
    drift_already_active = active_drift_count >= max_concurrent

    # 判定
    snapshot = DriftAdmissionSnapshot(
        active_normal_turns=active_turns,
        high_priority_task_backlog=priority_backlog,
        ready_delivery_backlog=delivery_backlog,
        outbox_critical_age_ms=outbox_age_ms,
        recovery_in_progress=recovery_in_progress,
        last_user_activity_age_ms=last_user_activity_age_ms,
        daily_drift_budget_remaining=daily_budget_remaining,
        drift_already_active=drift_already_active,
        snapshot_at=now_ms,
    )

    if active_turns > 0:
        reasons.append(DriftReasonCode.active_turn)
    if priority_backlog >= high_priority_backlog_threshold:
        reasons.append(DriftReasonCode.priority_backlog)
    if delivery_backlog > 0:
        reasons.append(DriftReasonCode.delivery_backlog)
    # outbox_critical_age 必须 < threshold 才准入；无 pending 视为满足
    if oldest_outbox_age_ms is not None and oldest_outbox_age_ms >= outbox_critical_age_ms:
        reasons.append(DriftReasonCode.outbox_critical)
    if recovery_in_progress:
        reasons.append(DriftReasonCode.recovery_in_progress)
    if daily_budget_remaining <= 0:
        reasons.append(DriftReasonCode.budget_exhausted)
    if drift_already_active:
        reasons.append(DriftReasonCode.drift_already_active)
    if (
        last_user_activity_age_ms is not None
        and last_user_activity_age_ms < idle_after_minutes * 60 * 1000
    ):
        reasons.append(DriftReasonCode.not_idle_long_enough)

    admit = len(reasons) == 0
    return DriftAdmissionResult(admit=admit, reasons=reasons, snapshot=snapshot)
