"""drift.run Handler + finish_drift 强制收尾 (M4/M5)。

drift.run 是普通 Task (复用 TaskWorker/Lease/ResourceBudget)；本模块提供
handler 入口，执行一个已选 Skill 的"无模型"只读维护动作，然后通过
finish_drift 强制收尾 (status + summary + 同步 skill_state)。

Skill 没有值得做的事时返回 skipped/no_value，不强行执行。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from cogito.domain.drift import (
    DriftReasonCode,
    DriftRunStatus,
    DriftSkillManifest,
)
from cogito.domain.task import Task

_LOGGER = logging.getLogger(__name__)


@dataclass
class _RunContext:
    """drift.run 执行上下文。"""
    drift_run_id: str
    task: Task
    manifest: DriftSkillManifest
    conn: Any
    config_version_id: str = ""
    workspace_path: str = ""


def handle_drift_run(task: Task, ctx: Any) -> str:
    """drift.run Task handler 入口。

    从 TaskHandlerContext 定位 drift_run 记录 + Skill manifest，
    执行 Skill 动作（默认仅只读无模型分支），finish_drift 收尾。
    """
    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "drift.run skipped: no connection"

    run_id = _resolve_drift_run_id(conn, task)
    if run_id is None:
        return "drift.run skipped: no drift_run for task"

    manifest = _resolve_manifest(conn, run_id)
    if manifest is None:
        # 未知 skill → 不抛失败，标记 completed + skipped_no_value
        _finish_drift(conn, run_id, DriftRunStatus.completed,
                      summary="unknown skill (no manifest)",
                      reason_code=DriftReasonCode.skipped_no_value)
        return f"drift.run {run_id}: skipped (no manifest)"

    run = _RunContext(
        drift_run_id=run_id, task=task, manifest=manifest, conn=conn,
        config_version_id=getattr(ctx, "config_version_id", ""),
        workspace_path=getattr(ctx, "workspace_path", ""),
    )

    # 执行 Skill 的只读动作
    try:
        result = _execute_skill(run)
    except Exception:
        _LOGGER.exception("drift skill %s failed", manifest.name)
        _finish_drift(conn, run_id, DriftRunStatus.failed,
                      summary=f"exception in {manifest.name}",
                      reason_code=DriftReasonCode.failed)
        return f"drift.run {run_id}: failed"

    if result.get("no_value"):
        _finish_drift(conn, run_id, DriftRunStatus.completed,
                      summary="no_value", reason_code=DriftReasonCode.skipped_no_value,
                      internal_items=result.get("items", []))
        return f"drift.run {run_id}: skipped (no_value)"

    _finish_drift(
        conn, run_id, DriftRunStatus.completed,
        summary=result.get("summary", "done"),
        reason_code=DriftReasonCode.completed,
        internal_items=result.get("items", []),
    )
    return f"drift.run {run_id}: completed"


def _resolve_drift_run_id(conn, task: Task) -> str | None:
    """通过 task_id 找 drift_run_id。"""
    row = conn.execute(
        "SELECT drift_run_id FROM drift_runs WHERE task_id=?", (task.task_id,),
    ).fetchone()
    return row[0] if row else None


def _resolve_manifest(conn, run_id: str) -> DriftSkillManifest | None:
    """解析当前 Skill manifest（优先内置目录）。"""
    row = conn.execute(
        "SELECT skill_name FROM drift_runs WHERE drift_run_id=?", (run_id,),
    ).fetchone()
    if row is None:
        return None
    from cogito.service.drift_skill_catalog import load_builtin_skills
    catalog = load_builtin_skills()
    skill_name = row[0]
    if skill_name in catalog:
        return catalog[skill_name].manifest
    return None


def _execute_skill(run: _RunContext) -> dict[str, Any]:
    """执行 Skill 动作（无模型只读分支）。"""
    name = run.manifest.name
    if name == "proactive-policy-view-audit":
        return _skill_policy_view_audit(run)
    # 未知 skill → no_value
    return {"no_value": True, "summary": f"unknown skill {name}", "items": []}


def _skill_policy_view_audit(run: _RunContext) -> dict[str, Any]:
    """proactive-policy-view-audit: 读取 DB Policy，生成只读摘要。"""
    from cogito.store.proactive_repo import ProactivePolicyRepository
    policy = ProactivePolicyRepository(run.conn).get_current()
    summary = (f"policy v{policy.version} dry_run={policy.dry_run} "
               f"budget=({policy.max_pushes_per_hour}/h,{policy.max_pushes_per_day}/d)")
    return {
        "no_value": False,
        "summary": summary,
        "items": [{"kind": "policy_view", "policy_version": policy.version,
                   "dry_run": policy.dry_run}],
    }


def _finish_drift(conn, run_id: str, status: DriftRunStatus, *,
                  summary: str, reason_code: DriftReasonCode,
                  internal_items: list[dict] | None = None) -> None:
    """强制收尾：更新 drift_runs.status + finish_summary + 同步 skill_state。

    强制收尾协议：completed/skipped 可以没有后续 Checkpoint；
    paused/waiting 由调用方在外部写 Checkpoint（M5）。
    """
    now_ms = int(time.time() * 1000)
    items = internal_items or []
    result_ref = f"payload:drift-result:{run_id}" if items else None
    conn.execute(
        "UPDATE drift_runs SET status=?, finish_summary=?, finished_at=?, "
        " preemption_reason=?, result_ref=? WHERE drift_run_id=?",
        (status.value, summary, now_ms,
         reason_code.value if status not in (DriftRunStatus.completed,
                                             DriftRunStatus.failed) else None,
         result_ref, run_id),
    )
    # 同步 drift_skill_state
    row = conn.execute(
        "SELECT skill_name, skill_version, principal_id FROM drift_runs "
        "WHERE drift_run_id=?", (run_id,),
    ).fetchone()
    if row:
        try:
            from cogito.store.drift_repo import DriftSkillStateRepository
            DriftSkillStateRepository(conn).upsert(
                principal_id=row[2], skill_name=row[0], skill_version=row[1],
                last_status=status.value, last_run_at=now_ms, run_count=1,
            )
        except Exception:
            _LOGGER.warning("upsert drift_skill_state failed for %s", row[0])
    try:
        conn.commit()
    except Exception:
        conn.rollback()
