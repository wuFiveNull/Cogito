"""drift.run Handler + 多步执行循环 + 抢占 + 真正 resume (R7 / M5 接线)。

drift.run 是普通 Task (复用 TaskWorker/Lease/ResourceBudget)。
本模块实现：
1. 多步执行循环（max_steps / max_runtime_seconds）
2. 每步前 should_preempt_step 检查（lease / cancel / turn / backlog / budget）
3. 安全点写 DriftCheckpointV1 + 更新 drift_runs.status=paused + 释放 Lease（返回让 TaskWorker complete）
4. 真正的 resume：读 checkpoint → 校验版本 → 从 step_index+1 续跑
5. budget 跨 Attempt 累计（update_progress 读取后累加，不重置）

Skill 没有值得做的事时返回 skipped/no_value，不强行执行。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
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
    attempt_id: str = ""                 # 本次 TaskAttempt 的真实 id (P0-03)
    # resume 时携带
    resume_from_step: int = 0
    resume_completed_actions: list[str] = field(default_factory=list)
    resume_budget: dict[str, int] = field(default_factory=dict)
    resume_cursor: dict[str, Any] = field(default_factory=dict)
    lease_checker: Any = None  # Callable[[], bool] | None


def handle_drift_run(task: Task, ctx: Any) -> str:
    """drift.run Task handler 入口 (R7)。

    - 定位 drift_run + Skill manifest
    - 若 drift_run.status=paused 且有 result_ref checkpoint → 走 resume 路径
    - 否则从 step 0 启动多步循环
    - 每步前抢占检查；被抢占 → 安全点写 checkpoint + status=paused
    - 完成后 finish_drift 强制收尾
    """
    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "drift.run skipped: no connection"

    run_id = _resolve_drift_run_id(conn, task)
    if run_id is None:
        return "drift.run skipped: no drift_run for task"

    manifest = _resolve_manifest(conn, run_id)
    if manifest is None:
        _finish_drift(conn, run_id, DriftRunStatus.completed,
                      summary="unknown skill (no manifest)",
                      reason_code=DriftReasonCode.skipped_no_value)
        return f"drift.run {run_id}: skipped (no manifest)"

    run = _RunContext(
        drift_run_id=run_id, task=task, manifest=manifest, conn=conn,
        config_version_id=getattr(ctx, "config_version_id", ""),
        workspace_path=getattr(ctx, "workspace_path", ""),
        attempt_id=getattr(ctx, "_attempt_id", "") or "",
        lease_checker=getattr(ctx, "lease_checker", None),
    )

    # resume 检测
    run_row = conn.execute(
        "SELECT status, result_ref, budget_used_json, steps_taken "
        "FROM drift_runs WHERE drift_run_id=?", (run_id,),
    ).fetchone()
    if run_row and run_row["status"] == "paused" and run_row["result_ref"]:
        return _resume_drift_run(run, run_row)

    return _start_drift_run(run)


def _start_drift_run(run: _RunContext) -> str:
    """从 step 0 启动多步循环；resume 时由 run.resume_* 恢复状态 (P0-04)。"""
    from cogito.service.drift_preemption import should_preempt_step, write_checkpoint

    # P0-04：resume 时从 checkpoint 恢复 step/cursor/budget/actions
    step_index = run.resume_from_step
    completed_actions: list[str] = list(run.resume_completed_actions)
    budget_used: dict[str, int] = dict(run.resume_budget)
    if not budget_used:
        # 确保工具/模型计数器基准存在，避免后续累加 KeyError
        budget_used.setdefault("tool_calls", 0)
        budget_used.setdefault("model_calls", 0)
    cursor: dict[str, Any] = dict(run.resume_cursor)
    started_ms = int(time.time() * 1000)
    max_steps = max(1, run.manifest.max_steps)
    max_runtime_ms = max(1, run.manifest.max_runtime_seconds) * 1000

    # 读取 principal_id
    principal_row = run.conn.execute(
        "SELECT principal_id FROM drift_runs WHERE drift_run_id=?",
        (run.drift_run_id,),
    ).fetchone()
    principal_id = principal_row["principal_id"] if principal_row else "owner"

    while step_index < max_steps:
        # 时间耗尽 → 进入 wrap-up
        elapsed = int(time.time() * 1000) - started_ms
        if elapsed >= max_runtime_ms:
            break

        # ① 抢占检查（每步前）— lease_valid 由 lease_checker 提供（默认有效）
        lease_ok = True
        if run.lease_checker is not None:
            try:
                lease_ok = run.lease_checker()
            except Exception:
                lease_ok = False
        preempted, reason = should_preempt_step(
            run.conn, principal_id=principal_id,
            lease_valid=lease_ok, budget_remaining=_budget_remaining(run, budget_used),
        )
        if preempted:
            write_checkpoint(
                run.conn, drift_run_id=run.drift_run_id, task_id=run.task.task_id,
                attempt_id=run.attempt_id, skill_name=run.manifest.name,
                skill_version=run.manifest.version, step_index=step_index,
                cursor=cursor, completed_actions=completed_actions,
                budget_used=budget_used, config_version_id=run.config_version_id,
            )
            _finish_drift(run.conn, run.drift_run_id, DriftRunStatus.paused,
                          summary=f"preempted at step {step_index}",
                          reason_code=reason,
                          budget_used=budget_used, steps_taken=step_index,
                          result_ref=f"drift-check:{run.drift_run_id}:{step_index}")
            # P0-02：在暂停同一事务里创建 follow-up 任务（绕过 admission
            # 的 already-active 守门，因为 paused 状态会 block 重入）。
            if reason != DriftReasonCode.paused_budget_exhausted:
                _create_resume_followup(
                    run.conn,
                    resume_drift_run_id=run.drift_run_id,
                    resume_step=step_index, resume_cursor=cursor,
                    resume_budget=budget_used, completed_actions=completed_actions,
                    skill_name=run.manifest.name,
                    config_version_id=run.config_version_id,
                )
            reason_label = reason.value if hasattr(reason, "value") else str(reason)
            return f"drift.run {run.drift_run_id}: paused ({reason_label}, step {step_index})"

        # ② 执行单步
        try:
            result = _execute_skill_step(run, step_index, cursor)
        except Exception:
            _LOGGER.exception("drift skill %s step %s failed",
                              run.manifest.name, step_index)
            _finish_drift(run.conn, run.drift_run_id, DriftRunStatus.failed,
                          summary=f"exception at step {step_index}",
                          reason_code=DriftReasonCode.failed,
                          budget_used=budget_used, steps_taken=step_index + 1)
            return f"drift.run {run.drift_run_id}: failed (step {step_index})"

        # 累计 actions + budget + cursor
        action_name = result.get("action") or f"step-{step_index}"
        completed_actions.append(action_name)
        for k, v in (result.get("budget") or {}).items():
            budget_used[k] = budget_used.get(k, 0) + v
        if result.get("cursor") is not None:
            cursor = result["cursor"]
        step_index += 1

        # ③ 安全点写 checkpoint（含当前 budget/cursor/completed_actions 快照）
        write_checkpoint(
            run.conn, drift_run_id=run.drift_run_id, task_id=run.task.task_id,
            attempt_id=run.attempt_id, skill_name=run.manifest.name,
            skill_version=run.manifest.version, step_index=step_index,
            cursor=cursor, completed_actions=completed_actions,
            budget_used=budget_used, config_version_id=run.config_version_id,
        )

        # ④ Skill 主动宣告做完 / no_value
        if result.get("no_value"):
            _finish_drift(run.conn, run.drift_run_id, DriftRunStatus.completed,
                          summary="no_value", reason_code=DriftReasonCode.skipped_no_value,
                          budget_used=budget_used, steps_taken=step_index,
                          internal_items=result.get("items", []))
            return f"drift.run {run.drift_run_id}: skipped (no_value)"
        if result.get("done"):
            break

    # 正常完成（step 耗尽 / 时间耗尽 / Skill done）
    _finish_drift(run.conn, run.drift_run_id, DriftRunStatus.completed,
                  summary=f"done at step {step_index}",
                  reason_code=DriftReasonCode.completed,
                  budget_used=budget_used, steps_taken=step_index)
    return f"drift.run {run.drift_run_id}: completed ({step_index} steps)"


def _resume_drift_run(run: _RunContext, run_row: Any) -> str:
    """真正的 resume：读持久化 checkpoint 校验版本 → 从 pause step 续跑 (P0-02/04)。

    版本权威的 checkpoint 来源优先级：
    1. 持久化 task_checkpoints 表（写入时 snapshot，不受当前 config_version 影响）；
    2. follow-up Task payload_ref（P0-02 follow-up 携带）。
    当前 config_version 只作为「当前值」用于与历史做 diff，不可回退用作历史值，
   否则版本变化永远被掩盖。
    """
    from cogito.service.drift_preemption import DriftCheckpointV1
    from cogito.service.drift_preemption import validate_checkpoint_for_resume
    from cogito.store.drift_repo import DriftRunRepository
    from cogito.store.task_checkpoint_repo import TaskCheckpointRepository

    stored = TaskCheckpointRepository(run.conn).latest_for_task(run.task.task_id)
    # follow-up payload（P0-02）；仅作兜底，config_version 以 stored 为准
    payload: dict[str, Any] = {}
    payload_ref = getattr(run.task, "payload_ref", None)
    if payload_ref:
        try:
            payload = json.loads(payload_ref)
        except Exception:
            payload = {}

    # 以持久化 snapshot（stored）优先恢复 step/cursor/budget/actions
    stored_data: dict[str, Any] = (
        json.loads(stored.payload_json) if stored else {})
    resume_step = int(
        stored_data.get("step_index")
        or payload.get("resume_step")
        or _step_from_result_ref(
            run_row["result_ref"] if result_ref_idx(run_row) else None))
    resume_cursor = (stored_data.get("cursor")
                     or payload.get("resume_cursor", {}))
    resume_budget = (stored_data.get("budget_used")
                     or payload.get("resume_budget", {}))
    resume_actions = (stored_data.get("completed_actions")
                      or payload.get("completed_actions", []))

    # 校验：以 persistence 时刻 snapshot 的 config_version 与当前对比（P0-04）
    if stored is not None:
        compatible, reason = validate_checkpoint_for_resume(
            stored.payload_json,
            current_config_version_id=run.config_version_id,
            current_skill_version=run.manifest.version,
        )
    else:
        compatible, reason = True, ""
    if not compatible:
        DriftRunRepository(run.conn).update_status(
            run.drift_run_id, DriftRunStatus.needs_review.value,
            preemption_reason=f"resume incompatible: {reason}")
        return f"drift.run {run.drift_run_id}: needs_review ({reason})"

    # 恢复累计状态，由 _start_drift_run 消费（P0-04）
    run.resume_from_step = resume_step
    run.resume_budget = dict(resume_budget)
    run.resume_completed_actions = list(resume_actions)
    run.resume_cursor = dict(resume_cursor)

    # 从 resume_step 续跑（预算继续累计）
    resume_summary = _start_drift_run(run)
    return resume_summary + " [resumed]"


def result_ref_idx(run_row: Any) -> Any:
    """取 sqlite3.Row 的 result_ref 列值（兼容 dict / Row）。"""
    try:
        return run_row["result_ref"]
    except Exception:
        return None


def _step_from_result_ref(result_ref: Any) -> int:
    if result_ref is None:
        return 0
    try:
        return int(str(result_ref).rsplit(":", 1)[-1])
    except Exception:
        return 0


def _execute_skill_step(run: _RunContext, step_index: int,
                        cursor: dict[str, Any]) -> dict[str, Any]:
    """执行单个 Skill 的单步。返回步结果 dict。"""
    name = run.manifest.name
    if name == "proactive-policy-view-audit":
        return _step_policy_view_audit(run, step_index, cursor)
    # 未知 skill
    return {"done": True, "no_value": True, "action": "unknown",
            "cursor": cursor, "budget": {}, "items": []}


def _step_policy_view_audit(run: _RunContext, step_index: int,
                            cursor: dict[str, Any]) -> dict[str, Any]:
    """proactive-policy-view-audit 单步（两步完成）：step0 读 policy；step1 done。"""
    from cogito.store.proactive_repo import ProactivePolicyRepository
    policy = ProactivePolicyRepository(run.conn).get_current()
    summary = (f"policy v{policy.version} dry_run={policy.dry_run} "
               f"budget=({policy.max_pushes_per_hour}/h,{policy.max_pushes_per_day}/d)")
    if step_index == 0:
        return {"done": False, "no_value": False, "action": "read_policy",
                "cursor": {"policy_version": policy.version},
                "budget": {"tool_calls": 1}, "items": []}
    # step_index >= 1 → done
    return {"done": True, "no_value": False, "action": "summarize",
            "cursor": {"policy_version": policy.version},
            "budget": {"tool_calls": 1},
            "items": [{"kind": "policy_view", "policy_version": policy.version,
                       "dry_run": policy.dry_run, "summary": summary}],
            "summary": summary}


def _create_resume_followup(
    conn,
    *,
    resume_drift_run_id: str,
    resume_step: int,
    resume_cursor: dict[str, Any],
    resume_budget: dict[str, int],
    completed_actions: list[str],
    skill_name: str,
    config_version_id: str,
) -> str | None:
    """P0-02：暂停后创建 follow-up 任务，绕过 admission 的 already-active 守门。

    paused 状态会被 Scheduler.admit() 判为 drift_already_active 而拒绝重入。
    通过在同一事务内直接写一个新的 queued Task (origin=drift-resume)，并让
    Worker 领取新 Attempt，从 pause 的 step 续跑，实现"真正 resume"。
    """
    import uuid
    idempotency = f"drift-resume:{resume_drift_run_id}:{resume_step}"
    # 幂等：同一 pause 点不重复创建 follow-up
    existing = conn.execute(
        "SELECT task_id FROM tasks WHERE idempotency_key=?", (idempotency,),
    ).fetchone()
    if existing is not None:
        return existing[0]
    payload = {
        "resume_drift_run_id": resume_drift_run_id,
        "resume_step": resume_step,
        "resume_cursor": dict(resume_cursor),
        "resume_budget": dict(resume_budget),
        "completed_actions": list(completed_actions),
        "skill_name": skill_name,
        "config_version_id": config_version_id,
    }
    task_id = f"task-dr-{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO tasks "
        "(task_id, task_type, payload_ref, status, priority, "
        " idempotency_key, origin, created_at) "
        "VALUES (?,?,?,?, ?,?,?,?)",
        (task_id, "drift.run",
         __import__("json").dumps(payload, ensure_ascii=False),
         "queued", 5, idempotency, "drift-resume",
         int(time.time() * 1000)),
    )
    # 注意：不改动 drift_runs.task_id，保持与原始 task 的关联（任务 T 完成/关
    # 联不变）。follow-up 任务通过 payload.resume_drift_run_id 自我定位。
    conn.commit()
    _LOGGER.info("drift resume follow-up created: %s for run %s @step %s",
                 task_id, resume_drift_run_id, resume_step)
    return task_id


def _resolve_drift_run_id(conn, task: Task) -> str | None:
    """通过 task_id 找 drift_run_id；follow-up 任务通过 payload.resume_drift_run_id 定位。"""
    row = conn.execute(
        "SELECT drift_run_id FROM drift_runs WHERE task_id=?", (task.task_id,),
    ).fetchone()
    if row is not None:
        return row[0]
    # follow-up 任务 (origin=drift-resume) 带新的 task_id，由 payload 指向原始 run
    payload_ref = getattr(task, "payload_ref", None)
    if payload_ref:
        try:
            payload = json.loads(payload_ref)
        except Exception:
            payload = {}
        resume_run_id = payload.get("resume_drift_run_id")
        if resume_run_id:
            row = conn.execute(
                "SELECT drift_run_id FROM drift_runs WHERE drift_run_id=?",
                (resume_run_id,),
            ).fetchone()
            if row is not None:
                return row[0]
    return None


def _resolve_manifest(conn, run_id: str) -> DriftSkillManifest | None:
    """解析当前 Skill manifest（优先内置目录）。"""
    row = conn.execute(
        "SELECT skill_name FROM drift_runs WHERE drift_run_id=?", (run_id,),
    ).fetchone()
    if row is None:
        return None
    from cogito.service.drift_skill_catalog import load_builtin_skills
    catalog = load_builtin_skills()
    return catalog[row[0]].manifest if row[0] in catalog else None


def _budget_remaining(run: _RunContext, used: dict[str, int]) -> int:
    """简化：max_tool_calls - used_tool_calls。"""
    return max(0, run.manifest.max_tool_calls - used.get("tool_calls", 0))


def _finish_drift(conn, run_id: str, status: DriftRunStatus, *,
                  summary: str, reason_code: DriftReasonCode | str,
                  budget_used: dict[str, int] | None = None,
                  steps_taken: int = 0,
                  internal_items: list[dict] | None = None,
                  result_ref: str | None = None) -> None:
    """强制收尾：更新 drift_runs.status + 同步 skill_state。"""
    from cogito.store.drift_repo import DriftRunRepository

    rc = reason_code.value if hasattr(reason_code, "value") else str(reason_code)
    fields: dict[str, Any] = {"finish_summary": summary}
    if status == DriftRunStatus.paused:
        fields["preemption_reason"] = rc
    if result_ref is not None:
        fields["result_ref"] = result_ref
    DriftRunRepository(conn).update_status(run_id, status.value, **fields)

    # 累计 budget/steps
    if budget_used and steps_taken:
        DriftRunRepository(conn).update_progress(
            run_id, budget_used=budget_used, steps_taken=steps_taken)

    # 同步 drift_skill_state
    row = conn.execute(
        "SELECT skill_name, skill_version, principal_id FROM drift_runs WHERE drift_run_id=?",
        (run_id,),
    ).fetchone()
    if row:
        try:
            from cogito.store.drift_repo import DriftSkillStateRepository
            DriftSkillStateRepository(conn).upsert(
                principal_id=row[2], skill_name=row[0], skill_version=row[1],
                last_status=status.value,
                last_run_at=int(time.time() * 1000), run_count=1)
        except Exception:
            _LOGGER.warning("upsert drift_skill_state failed for %s", row[0])
