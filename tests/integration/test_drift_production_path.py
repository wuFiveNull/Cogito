"""PLAN-17 R0/R1/R2 生产路径探针。

P0-01：Scheduler admission 必须持久化真实 Skill（不是占位符），Runner 能真正执行。
P0-02：pause 必须创建 follow-up 任务（origin=drift-resume），让 Worker 领取新 Attempt
       resume，resume 从暂停 step 续跑（不是 step 0）。budget/cursor 跨 Attempt 累计。
"""
from __future__ import annotations

import sqlite3
import time

import json

from cogito.config import DriftConfig
from cogito.domain.task import Task, TaskStatus
from cogito.service.drift_preemption import request_preemption, \
    should_preempt_step, write_checkpoint
from cogito.service.drift_runner import handle_drift_run
from cogito.service.scheduler import Scheduler
from cogito.service.task_dispatcher import TaskDispatcher
from cogito.store.migration import migrate
from cogito.store.task_repo import TaskRepository


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


class _Ctx:
    def __init__(self, c, attempt_id=""):
        self.connection_factory = lambda p=c: p
        self.config_version_id = "cfg-prod"
        self.workspace_path = ""
        self._attempt_id = attempt_id


def test_admission_persists_real_skill_and_runner_executes():
    """Scheduler → admission → 真实 Skill；Runner 真正执行并 completed。"""
    conn = _fresh_db()
    # 清理可能残存的 drift 状态，避免 drift_already_active 拦截
    dr_cfg = DriftConfig(
        enabled=True, dry_run=False,
        default_principal_id="owner",
        idle_after_minutes=30,
        max_runs_per_day=3,
    )
    scheduler = Scheduler(conn, drift_config=dr_cfg, workspace_path="",)

    # admission tick —— 必须创建任务 + drift_run 并持久真实 skill_name
    out = scheduler.tick_drift_admit()
    assert out is not None, "admission 应该通过（空闲、无抢占、预算充足）"
    run_id, task_id = out

    run_row = conn.execute(
        "SELECT skill_name, skill_version, selection_trace_json, selector_version "
        "FROM drift_runs WHERE drift_run_id=?", (run_id,),
    ).fetchone()
    assert run_row is not None
    assert run_row["skill_name"] and run_row["skill_name"] != "(selected-at-run)", \
        "占位符必须被替换为真实 Skill"
    assert run_row["skill_version"] == "1.0"
    # 选择追溯已持久化
    assert run_row["selection_trace_json"], "selection_trace_json 必须非空"
    assert '"scores"' in run_row["selection_trace_json"]
    assert run_row["selector_version"] == "1"

    # Runner 必须真正执行该 Skill（不再 UNKNOWN skill / skipped）
    task = Task(task_id=task_id, task_type="drift.run",
                status=TaskStatus.running)
    result = handle_drift_run(task, _Ctx(conn))
    assert "unknown skill" not in result, \
        f"Runner 不应该把已选 Skill 当 unknown 处理：{result}"
    assert "completed" in result, f"read-only Skill 应正常 completed：{result}"

    steps_row = conn.execute(
        "SELECT steps_taken FROM drift_runs WHERE drift_run_id=?", (run_id,),
    ).fetchone()
    assert steps_row["steps_taken"] >= 1, \
        "真实 Skill 至少执行了 1 步"


def test_worker_claim_binds_attempt_id_to_checkpoint():
    """DR-P0-03: Worker.claim_next 创建的 TaskAttempt 必须被写进 checkpoint；
    task_attempts 里会有对应的 running attempt，checkpoint 的 attempt_id 与其绑定。"""
    conn = _fresh_db()
    now = int(time.time() * 1000)
    scheduler = Scheduler(
        conn,
        drift_config=DriftConfig(
            enabled=True, dry_run=False,
            default_principal_id="owner",
            idle_after_minutes=30, max_runs_per_day=3),
        workspace_path="")
    out = scheduler.tick_drift_admit()
    assert out is not None
    run_id, task_id = out

    dispatcher = TaskDispatcher(conn)
    worker_id = "wkr-attempt"
    claimed = dispatcher.claim_next(worker_id)
    assert claimed is not None, "Worker 应 claim 到 drift.run 任务"
    task, attempt = claimed.task, claimed.attempt
    assert task.task_id == task_id
    assert attempt.task_attempt_id, "TaskAttempt 必须有真实 id"
    assert attempt.status.value == "running"

    # 模拟 Worker：把 attempt_id 注入 handler ctx（同 task_worker.py 实际注入）
    ctx = _Ctx(conn, attempt_id=attempt.task_attempt_id)
    result = handle_drift_run(task, ctx)
    assert "completed" in result, f"read-only Skill 应 completed：{result}"

    # checkpoint row 里 attempt_id 绑定到真实 attempt（非空、非 fallback 解析失误）
    ck = conn.execute(
        "SELECT task_attempt_id, payload_json FROM task_checkpoints "
        "WHERE drift_run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,),
    ).fetchone()
    assert ck is not None
    assert ck["task_attempt_id"] == attempt.task_attempt_id, \
        f"checkpoint 必须绑定真实 attempt_id，got={ck['task_attempt_id']}"
    data = json.loads(ck["payload_json"])
    assert data["attempt_id"] == attempt.task_attempt_id
    # task_attempts.checkpoint_ref 同步
    att_row = conn.execute(
        "SELECT checkpoint_ref FROM task_attempts "
        "WHERE task_attempt_id=?", (attempt.task_attempt_id,),
    ).fetchone()
    assert att_row is not None and att_row["checkpoint_ref"], \
        "Attempt 的 checkpoint_ref 必须指向 checkpoint"


def test_pause_creates_followup_and_resume_runs_from_paused_step():
    """P0-02：在 step 1 处抢占暂停 → 创建 follow-up 任务 → Worker 领取 resume，
    从 step 1 续跑，budget/cursor 累计。"""
    conn = _fresh_db()

    # 手工 seed：让 Skill 在 step 0 执行成功、step 1 被抢占暂停
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, priority, "
        "idempotency_key, created_at) VALUES (?,?,?,?,?,?)",
        ("t-base", "drift.run", "running", 5, "id-p02",
         int(time.time()*1000)),
    )
    conn.execute(
        "INSERT INTO drift_runs (drift_run_id, task_id, principal_id, "
        "skill_name, skill_version, status, admission_snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("dr-p02", "t-base", "owner", "proactive-policy-view-audit",
         "1.0", "running", "{}", int(time.time()*1000)),
    )
    conn.commit()

    dr_cfg = DriftConfig(enabled=True, dry_run=False)
    scheduler = Scheduler(conn, drift_config=dr_cfg, workspace_path="")
    claim = TaskDispatcher(conn)
    worker_id = "wkr-p02"
    attempt_no = [0]

    # 用真实 Runner 多步循环，但在 step1 之前强制抢占：通过装饰 runner.conn
    # 的 request_preemption 是不可见的；转而直接对 run 注入一个 lease_checker，
    # 在 step0 后返回 False ⇒ should_preempt_step 判定 lease_lost ⇒ pause。
    task = conn.execute("SELECT * FROM tasks WHERE task_id='t-base'").fetchone()
    # 改为仿照调度器：step0 成功后，触发抢占 signal，再跑 step1 前被捕捉。
    # 为了精准在 step 1 暂停，我们在 step 0 之后写入 preemption signal。
    # 但这需要重新驱动 run：使用一次调用让其自然跑完 step0 后，
    # 由于 proactive-policy-view-audit 是 2 步 done，需要手动注入抢占。

    # 更直接：通过 _Ctx 注入 lease_checker，在第 1 次步检查时使 lease 失效。
    resume_cfg_version = "cfg-p02"

    class _Ctx2:
        def __init__(self, c):
            self.connection_factory = lambda p=c: p
            self.config_version_id = resume_cfg_version
            self.workspace_path = ""
            self._calls = 0
            self.lease_checker = self._checker
        def _checker(self):
            self._calls += 1
            # 第 1 次检查（step 0 前）lease 有效；第 2 次（step 1 前）失效 → pause
            return self._calls <= 1

    class _CtxResume:
        def __init__(self, c, cfg):
            self.connection_factory = lambda p=c: p
            self.config_version_id = cfg
            self.workspace_path = ""

    task_obj = Task(task_id="t-base", task_type="drift.run",
                    status=TaskStatus.running)
    first = handle_drift_run(task_obj, _Ctx2(conn))
    assert "paused" in first, f"应 pause 在 step1 前：{first}"

    # 验证：drift_run status=paused，step 至少 0（step0 已成功执行并入 checkpoint）
    run = conn.execute(
        "SELECT status, steps_taken, budget_used_json, result_ref "
        "FROM drift_runs WHERE drift_run_id='dr-p02'").fetchone()
    assert run["status"] == "paused"

    # P0-02：必须有 follow-up 任务生成（origin=drift-resume）
    followup = conn.execute(
        "SELECT task_id, payload_ref, origin FROM tasks "
        "WHERE origin='drift-resume' AND task_id!='t-base'").fetchone()
    assert followup is not None, "pause 必须创建 follow-up Task"
    payload = json.loads(followup["payload_ref"])
    assert payload["resume_step"] == 1
    assert payload["skill_name"] == "proactive-policy-view-audit"

    # 模拟 Worker：领取 follow-up 并 resume
    got = claim.claim_next(worker_id)
    assert got is not None, "Worker 应领取到 follow-up Task"
    fup_task, fup_attempt = got.task, got.attempt
    assert fup_task.task_id == followup["task_id"]
    # attempt_no 递增（新 Attempt）
    assert fup_attempt.attempt_no == run["steps_taken"] or fup_attempt.attempt_no >= 1

    second = handle_drift_run(fup_task, _CtxResume(conn, resume_cfg_version))
    # resume 必须从 step1 续跑，最终 [resumed] + completed
    assert "resumed" in second.lower(), f"resume 必须标注 [resumed]：{second}"
    assert "completed" in second.lower(), f"resume 后应完成：{second}"

    # DR-P0-04：跨 Attempt 预算/step 精确，不重置、不双重累计
    run2 = conn.execute(
        "SELECT budget_used_json, steps_taken FROM drift_runs "
        "WHERE drift_run_id='dr-p02'").fetchone()
    budget = json.loads(run2["budget_used_json"])
    assert budget.get("tool_calls", 0) == 2, \
        f"跨 Attempt 预算应精确为 2（无双重累计）：{budget}"
    assert run2["steps_taken"] == 2, \
        f"跨 Attempt 步数应精确为 2：{run2['steps_taken']}"
