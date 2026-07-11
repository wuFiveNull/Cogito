"""Drift 抢占与恢复 (M5 / DR-P0-03)。

每步执行前检查：lease_valid / cancel-preempt_requested / active_normal_turns /
priority_backlog / budget_remaining。新 Turn 入站后发出 preemption signal，
Drift 在安全点写 DriftCheckpointV1 + 更新 TaskAttempt.checkpoint_ref + 释放 Lease。

恢复前校验 config_version_id / skill_version / checkpoint_schema_version。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from cogito.domain.drift import (
    DriftCheckpointV1,
    DriftReasonCode,
    DriftRunStatus,
)

_LOGGER = logging.getLogger(__name__)

# preemption signal 表由 migration 0049 创建；此处不再重复建表。


def request_preemption(conn, principal_id: str, reason: str) -> None:
    """新 Turn 入站后调用：置位 preemption signal。"""
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO drift_preemption_signals "
        "(principal_id, preempt_requested, requested_at, reason) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(principal_id) DO UPDATE SET "
        "preempt_requested=1, requested_at=excluded.requested_at, "
        "reason=excluded.reason",
        (principal_id, 1, now, reason),
    )
    conn.commit()


def is_preemption_requested(conn, principal_id: str) -> tuple[bool, str]:
    """检查并清除 preemption signal。"""
    row = conn.execute(
        "SELECT preempt_requested, reason FROM drift_preemption_signals "
        "WHERE principal_id=?", (principal_id,),
    ).fetchone()
    if row and row[0]:
        # 消费后清除
        conn.execute(
            "UPDATE drift_preemption_signals SET preempt_requested=0 WHERE principal_id=?",
            (principal_id,),
        )
        conn.commit()
        return True, (row[1] or "")
    return False, ""


def should_preempt_step(
    conn,
    *,
    principal_id: str,
    lease_valid: bool,
    budget_remaining: int,
    active_normal_turns: int = 0,
    priority_backlog: int = 0,
) -> tuple[bool, str]:
    """Drift 单步前检查。返回 (should_preempt, reason)。"""
    if not lease_valid:
        return True, DriftReasonCode.lease_lost
    preempted, reason = is_preemption_requested(conn, principal_id)
    if preempted:
        return True, DriftReasonCode.preempted_by_turn
    if active_normal_turns > 0:
        return True, DriftReasonCode.active_turn
    if priority_backlog > 0:
        return True, DriftReasonCode.priority_backlog
    if budget_remaining <= 0:
        return True, DriftReasonCode.paused_budget_exhausted
    return False, ""


def write_checkpoint(
    conn,
    *,
    drift_run_id: str,
    task_id: str,
    attempt_id: str,
    skill_name: str,
    skill_version: str,
    step_index: int,
    cursor: dict[str, Any],
    completed_actions: list[str],
    budget_used: dict[str, int],
    config_version_id: str,
    capability_snapshot_version: str = "",
) -> str:
    """写 DriftCheckpointV1 到 payload_ref 风格的 JSON (返回 JSON 字符串)。

    并更新 drift_runs 行的 result_ref 指向该 checkpoint。
    """
    ck = DriftCheckpointV1(
        drift_run_id=drift_run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        skill_name=skill_name,
        skill_version=skill_version,
        step_index=step_index,
        cursor=dict(cursor),
        completed_actions=list(completed_actions),
        budget_used=dict(budget_used),
        config_version_id=config_version_id,
        capability_snapshot_version=capability_snapshot_version,
    )
    data = ck.to_dict()
    ref = f"drift-check:{drift_run_id}:{step_index}"
    conn.execute(
        "UPDATE drift_runs SET result_ref=? WHERE drift_run_id=?",
        (ref, drift_run_id),
    )
    conn.commit()
    return json.dumps(data, ensure_ascii=False)


def validate_checkpoint_for_resume(
    checkpoint_json: str,
    *,
    current_config_version_id: str,
    current_skill_version: str,
) -> tuple[bool, str]:
    """恢复前校验：config/skill/checkpoint schema 版本兼容。

    不兼容 → (False, reason)。
    """
    try:
        data = json.loads(checkpoint_json)
    except Exception:
        return False, "invalid checkpoint json"
    schema = data.get("schema_version")
    if schema != 1:
        return False, f"incompatible checkpoint schema_version={schema}"
    if (data.get("config_version_id")
            and data["config_version_id"] != current_config_version_id):
        return False, "config_version changed"
    if (data.get("skill_version")
            and data["skill_version"] != current_skill_version):
        return False, "skill_version changed"
    return True, ""
