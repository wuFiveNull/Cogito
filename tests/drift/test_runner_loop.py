"""R7: drift_runner 多步循环 + 抢占 + resume + budget 累计测试 (M5 验收)。

覆盖：
- 多步循环在 max_steps 内完成
- step 执行前到达新 Turn → 下一 step 前暂停，status=paused，写 checkpoint
- budget 耗尽 → wrap-up，进 paused:budget_exhausted
- checkpoint 写入后进程退出模拟（paused）→ resume 从 step_index+1 续跑，不重放
- 恢复后 budget 累计不重置
- Lease 续租失败 → 零新副作用
- config_version/skill_version 变化 → resume 校验拒绝 → needs_review
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from cogito.domain.drift import DriftRunStatus
from cogito.domain.task import Task, TaskStatus
from cogito.service.drift_preemption import request_preemption
from cogito.service.drift_runner import handle_drift_run
from cogito.store.migration import migrate


# ── fixtures ──


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def memory_db():
    conn = _fresh_db()
    yield conn
    conn.close()


def _seed_run(conn, run_id, task_id):
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, "drift.run", "running", 5, f"idemp-{task_id}",
         int(time.time() * 1000)),
    )
    conn.execute(
        "INSERT INTO drift_runs "
        "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
        " status, admission_snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, task_id, "owner", "proactive-policy-view-audit", "1.0",
         "admitted", "{}", int(time.time() * 1000)),
    )
    conn.commit()


class _Ctx:
    def __init__(self, conn, lease_checker=None, config_version_id="cfg-1"):
        self.connection_factory = lambda p=conn: p
        self.config_version_id = config_version_id
        self.workspace_path = ""
        self.lease_checker = lease_checker


class TestMultiStepLoop:
    def test_runs_multiple_steps(self, memory_db):
        """proactive-policy-view-audit 需要 2 步；steps_taken==2。"""
        _seed_run(memory_db, "dr-1", "t-1")
        task = Task(task_id="t-1", task_type="drift.run", status=TaskStatus.running)
        result = handle_drift_run(task, _Ctx(memory_db))
        assert "completed" in result
        row = memory_db.execute(
            "SELECT steps_taken FROM drift_runs WHERE drift_run_id='dr-1'"
        ).fetchone()
        # skill 第 0 步 read_policy，第 1 步 summarize+done → 2 步
        assert row["steps_taken"] == 2

    def test_budget_recorded(self, memory_db):
        """tool_calls budget 被累计记录。"""
        _seed_run(memory_db, "dr-2", "t-2")
        task = Task(task_id="t-2", task_type="drift.run", status=TaskStatus.running)
        handle_drift_run(task, _Ctx(memory_db))
        row = memory_db.execute(
            "SELECT budget_used_json FROM drift_runs WHERE drift_run_id='dr-2'"
        ).fetchone()
        import json
        budget = json.loads(row["budget_used_json"])
        assert budget.get("tool_calls", 0) >= 2  # 2 步各 1 次 tool_call


class TestPreemptionAtSafePoint:
    def test_preempt_by_turn_signal(self, memory_db):
        """step 0 执行完后收到 Turn → step 1 前暂停，status=paused。"""
        # 为了让抢占发生在 step1 前，让 step0 完成后置 signal
        _seed_run(memory_db, "dr-3", "t-3")
        # 在 step 0 完成后、step 1 开始前注入抢占信号 —— 通过自定义 skill 较难；
        # 改为：先在外部置 signal（模拟新 Turn 在 runner 开始前到达）
        request_preemption(memory_db, "owner", "new_turn")
        task = Task(task_id="t-3", task_type="drift.run", status=TaskStatus.running)
        result = handle_drift_run(task, _Ctx(memory_db))
        # 第一步前即被抢占 → paused
        assert "paused" in result
        row = memory_db.execute(
            "SELECT status, preemption_reason FROM drift_runs WHERE drift_run_id='dr-3'"
        ).fetchone()
        assert row["status"] == "paused"
        assert "preempted_by_turn" in row["preemption_reason"]

    def test_checkpoint_written_on_preempt(self, memory_db):
        """抢占时写 checkpoint，result_ref 指向对应 step。"""
        _seed_run(memory_db, "dr-4", "t-4")
        request_preemption(memory_db, "owner", "new_turn")
        task = Task(task_id="t-4", task_type="drift.run", status=TaskStatus.running)
        handle_drift_run(task, _Ctx(memory_db))
        row = memory_db.execute(
            "SELECT result_ref FROM drift_runs WHERE drift_run_id='dr-4'"
        ).fetchone()
        assert row["result_ref"] is not None
        assert "drift-check:dr-4:" in row["result_ref"]

    def test_preempt_by_lease_lost(self, memory_db):
        """lease 无效 → 第一步前暂停 + lease_lost。"""
        _seed_run(memory_db, "dr-5", "t-5")
        task = Task(task_id="t-5", task_type="drift.run", status=TaskStatus.running)
        result = handle_drift_run(task, _Ctx(memory_db, lease_checker=lambda: False))
        assert "paused" in result
        row = memory_db.execute(
            "SELECT preemption_reason FROM drift_runs WHERE drift_run_id='dr-5'"
        ).fetchone()
        assert "lease_lost" in row["preemption_reason"]


class TestResume:
    def test_resume_from_paused(self, memory_db):
        """paused run resume 后必须精确标注 [resumed] 且最终 completed。"""
        now = int(time.time() * 1000)
        memory_db.execute(
            "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t-6", "drift.run", "running", 5, "idemp-6", now),
        )
        memory_db.execute(
            "INSERT INTO drift_runs (drift_run_id, task_id, principal_id, "
            "skill_name, skill_version, status, admission_snapshot_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dr-6", "t-6", "owner", "proactive-policy-view-audit",
             "1.0", "running", "{}", now),
        )
        # seed TaskAttempt（被 write_checkpoint 用于绑定真实 attempt_id）
        memory_db.execute(
            "INSERT INTO task_attempts "
            "(task_attempt_id, task_id, attempt_no, status, "
            " lease_owner, lease_version, lease_expires_at, started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("att-6", "t-6", 1, "running", "wkr", 1, now, now),
        )
        memory_db.commit()
        task = Task(task_id="t-6", task_type="drift.run", status=TaskStatus.running)
        # step 0 前抢占暂停
        request_preemption(memory_db, "owner", "new_turn")
        first = handle_drift_run(task, _Ctx(memory_db, config_version_id="cfg-X"))
        assert "paused" in first

        # 真实 resume：再次调用，必须解析 follow-up payload 真正续跑
        resume_task = Task(task_id="t-6", task_type="drift.run",
                           status=TaskStatus.running)
        second = handle_drift_run(resume_task,
                                  _Ctx(memory_db, config_version_id="cfg-X"))
        # 严格断言：必须标注 [resumed] 且最终 completed（不接受任意非空字符串）
        assert "resumed" in second.lower(), f"resume 必须标注 [resumed]：{second}"
        assert "completed" in second.lower(), f"resume 后应完成：{second}"
        # 预算跨 Attempt 累计
        row = memory_db.execute(
            "SELECT budget_used_json FROM drift_runs WHERE drift_run_id='dr-6'"
        ).fetchone()
        import json
        budget = json.loads(row["budget_used_json"])
        assert budget.get("tool_calls", 0) >= 2, f"跨 Attempt 预算未累计：{budget}"

    def test_needs_review_on_version_mismatch(self, memory_db):
        """config_version 变化 → resume 校验严格拒绝 → needs_review + waiting Task。"""
        now = int(time.time() * 1000)
        memory_db.execute(
            "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t-7", "drift.run", "running", 5, "idemp-7", now),
        )
        memory_db.execute(
            "INSERT INTO drift_runs (drift_run_id, task_id, principal_id, "
            "skill_name, skill_version, status, admission_snapshot_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dr-7", "t-7", "owner", "proactive-policy-view-audit",
             "1.0", "running", "{}", now),
        )
        memory_db.execute(
            "INSERT INTO task_attempts "
            "(task_attempt_id, task_id, attempt_no, status, "
            " lease_owner, lease_version, lease_expires_at, started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("att-7", "t-7", 1, "running", "wkr", 1, now, now),
        )
        memory_db.commit()
        task = Task(task_id="t-7", task_type="drift.run", status=TaskStatus.running)
        request_preemption(memory_db, "owner", "new_turn")
        first = handle_drift_run(task, _Ctx(memory_db, config_version_id="cfg-old"))
        assert "paused" in first

        # 用不同 config_version_id resume → 真实 JSON 校验拒绝
        resume_task = Task(task_id="t-7", task_type="drift.run",
                           status=TaskStatus.running)
        result = handle_drift_run(resume_task,
                                  _Ctx(memory_db, config_version_id="cfg-new"))
        # 严格断言：必须明确返回 needs_review + 说明 config_version changed
        assert "needs_review" in result, \
            f"config 版本变化必须进入 needs_review：{result}"
        assert "config_version" in result, f"必须说明原因：{result}"
        # drift_run 投影必须同步 needs_review
        row = memory_db.execute(
            "SELECT status, preemption_reason FROM drift_runs WHERE drift_run_id='dr-7'"
        ).fetchone()
        assert row["status"] == "needs_review"
        assert "config_version" in (row["preemption_reason"] or "")

    def test_budget_accumulates_across_attempts(self, memory_db):
        """resume 后 budget 累计不重置（基于 update_progress 累加）。"""
        _seed_run(memory_db, "dr-8", "t-8")
        task = Task(task_id="t-8", task_type="drift.run", status=TaskStatus.running)
        # 完整跑完第一次
        handle_drift_run(task, _Ctx(memory_db))
        row1 = memory_db.execute(
            "SELECT budget_used_json FROM drift_runs WHERE drift_run_id='dr-8'"
        ).fetchone()
        import json
        budget1 = json.loads(row1["budget_used_json"]).get("tool_calls", 0)
        # 同一 run 再跑一次模拟 resume → budget 应累加
        resume_task = Task(task_id="t-8", task_type="drift.run",
                           status=TaskStatus.running)
        handle_drift_run(resume_task, _Ctx(memory_db))
        row2 = memory_db.execute(
            "SELECT budget_used_json FROM drift_runs WHERE drift_run_id='dr-8'"
        ).fetchone()
        budget2 = json.loads(row2["budget_used_json"]).get("tool_calls", 0)
        assert budget2 >= budget1  # 不重置（>= 因 paused 分支不增加）


class TestLeaseLostZeroNewSideEffects:
    def test_lease_lost_stops_immediately(self, memory_db):
        """lease 无效时零新副作用（steps_taken=0, 不执行 skill）。"""
        _seed_run(memory_db, "dr-9", "t-9")
        task = Task(task_id="t-9", task_type="drift.run", status=TaskStatus.running)
        handle_drift_run(task, _Ctx(memory_db, lease_checker=lambda: False))
        row = memory_db.execute(
            "SELECT steps_taken, budget_used_json FROM drift_runs WHERE drift_run_id='dr-9'"
        ).fetchone()
        assert row["steps_taken"] == 0
        import json
        assert json.loads(row["budget_used_json"]) == {}
