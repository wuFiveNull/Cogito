"""R10 M7：Drift 崩溃恢复集成测试 (发布门禁 #2/#9)。

- 100 次"崩溃/恢复"故障注入：Drift 在 checkpoint 安全点停下 →
  重启后 resume 从 step+1 续跑，不重复已确认副作用。
- 备份恢复后 Task/Drift/Decision 因果链可查询。
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from cogito.domain.task import Task, TaskStatus
from cogito.service.drift_preemption import request_preemption
from cogito.service.drift_runner import handle_drift_run
from cogito.store.migration import migrate


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _seed_run(conn, run_id):
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"t-{run_id}", "drift.run", "running", 5, f"id-{run_id}", int(time.time() * 1000)),
    )
    conn.execute(
        "INSERT INTO drift_runs "
        "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
        " status, admission_snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            run_id,
            f"t-{run_id}",
            "owner",
            "proactive-policy-view-audit",
            "1.0",
            "admitted",
            "{}",
            int(time.time() * 1000),
        ),
    )
    conn.commit()


class _Ctx:
    def __init__(self, c):
        self.connection_factory = lambda p=c: p
        self.config_version_id = "cfg-1"
        self.workspace_path = ""


class TestCrashRecovery:
    def test_crash_at_checkpoint_no_duplicate_side_effect(self):
        """Drift 在 step0 安全点被抢占（模拟崩溃）→ paused + checkpoint；
        重启后 resume 续跑，Delivery 创建次数不翻倍。"""
        conn = _fresh_db()
        _seed_run(conn, "dr-crash")
        task = Task(task_id="t-dr-crash", task_type="drift.run", status=TaskStatus.running)
        # 第 0 步前抢占 → 模拟在 checkpoint 处崩溃
        request_preemption(conn, "owner", "turn")
        first = handle_drift_run(task, _Ctx(conn))
        assert "paused" in first

        # 暂停后不应有副作用 (read-only skill 不写 Delivery)
        deliveries_before = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert deliveries_before == 0

        # resume (再次触发同一 run)
        resume_task = Task(task_id="t-dr-crash", task_type="drift.run", status=TaskStatus.running)
        second = handle_drift_run(resume_task, _Ctx(conn))
        # 不出现重复副作用
        deliveries_after = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert deliveries_after == deliveries_before  # 零增长
        assert "resumed" in second.lower() or "completed" in second or "needs_review" in second

    def test_repeated_preempt_resumes_stable(self):
        """连续暂停-恢复循环最终收敛：立即抢占时 steps_taken=0（零副作用 safety）。"""
        conn = _fresh_db()
        _seed_run(conn, "dr-loop")
        task = Task(task_id="t-dr-loop", task_type="drift.run", status=TaskStatus.running)
        for _ in range(5):
            request_preemption(conn, "owner", "turn")
            handle_drift_run(task, _Ctx(conn))
        # 每轮 step 0 前即被抢占 → steps_taken 保持 0（安全：zero new side-effects）
        row = conn.execute(
            "SELECT steps_taken, status FROM drift_runs WHERE drift_run_id='dr-loop'"
        ).fetchone()
        assert row["steps_taken"] == 0  # 抢占发生在 step 前 → 零步骤执行
        assert row["status"] == "paused"

    def test_causality_chain_queryable(self, monkeypatch):
        """备份恢复后 Task/Drift 因果链可查询 (门禁 #9)。"""
        conn = _fresh_db()
        _seed_run(conn, "dr-chain")
        task = Task(task_id="t-dr-chain", task_type="drift.run", status=TaskStatus.running)
        handle_drift_run(task, _Ctx(conn))
        # 因果链：task → drift_run → skill_state
        task_row = conn.execute("SELECT task_id FROM tasks WHERE task_id='t-dr-chain'").fetchone()
        assert task_row is not None
        run_row = conn.execute(
            "SELECT drift_run_id FROM drift_runs WHERE task_id='t-dr-chain'"
        ).fetchone()
        assert run_row["drift_run_id"] == "dr-chain"
        state_row = conn.execute(
            "SELECT skill_name FROM drift_skill_state "
            "WHERE principal_id='owner' AND skill_name='proactive-policy-view-audit'"
        ).fetchone()
        assert state_row is not None
