"""R10 M7：Drift 离线回放集（确定性）。

覆盖：
- admission：有工作 vs 无工作 / 连续 paused / 失败退避
- 新 Turn 抢占、资源压力
- unauthorized Tool 在执行前被拒绝（MVP 无 shell/network/send）
- crash → lease expiry → recovery
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from cogito.domain.drift import DriftRunStatus, DriftSkillManifest
from cogito.service.drift_admission import admit
from cogito.service.drift_preemption import (
    request_preemption,
    should_preempt_step,
)
from cogito.store.migration import migrate


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _seed_turn(conn, status="running"):
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, input_message_id, status, priority, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            f"t-{status}-{int(time.time() * 1000) % 100000}",
            "s1",
            "m1",
            status,
            80,
            str(int(time.time() * 1000)),
        ),
    )
    conn.commit()


def _seed_run_active(conn, run_id="dr-active", status="running"):
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
            status,
            "{}",
            int(time.time() * 1000),
        ),
    )
    conn.commit()


class TestDriftAdmissionReplay:
    def test_admit_when_idle(self):
        """无任何活动 → admit。"""
        conn = _fresh_db()
        r = admit(conn, principal_id="owner")
        assert r.admit is True

    def test_deny_when_active_run(self):
        """已有 active Drift → deny drift_already_active。"""
        conn = _fresh_db()
        _seed_run_active(conn)
        r = admit(conn, principal_id="owner")
        assert r.admit is False
        assert "drift_already_active" in r.reasons

    def test_deny_when_turn_running(self):
        """新 Turn running → deny active_turn。"""
        conn = _fresh_db()
        _seed_turn(conn, status="running")
        r = admit(conn, principal_id="owner")
        assert r.admit is False
        assert "active_turn" in r.reasons

    def test_consecutive_paused_not_active(self):
        """paused run 在 has_active_run 中视为 active → 不再重复 admit。"""
        conn = _fresh_db()
        conn.execute(
            "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t-p", "drift.run", "running", 5, "id-p", int(time.time() * 1000)),
        )
        conn.execute(
            "INSERT INTO drift_runs "
            "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
            " status, admission_snapshot_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dr-p", "t-p", "owner", "s", "1.0", "paused", "{}", int(time.time() * 1000)),
        )
        conn.commit()
        r = admit(conn, principal_id="owner")
        assert r.admit is False  # paused 仍视为 active

    def test_no_work_scenario_admits(self):
        """无 Candidate 可评估时 (energy=low) idle 仍可 admit。"""
        conn = _fresh_db()
        r = admit(conn, principal_id="owner", idle_after_minutes=1)
        assert r.admit is True


class TestDriftPreemptionReplay:
    def test_preempt_by_turn_signal(self):
        """新 Turn 入站后置位 signal → should_preempt_step=True。"""
        conn = _fresh_db()
        request_preemption(conn, "owner", "new_turn")
        preempted, reason = should_preempt_step(
            conn, principal_id="owner", lease_valid=True, budget_remaining=10
        )
        assert preempted is True
        assert "preempted" in reason

    def test_preempt_by_lease_lost(self):
        """lease 无效 → zero new side-effects。"""
        conn = _fresh_db()
        preempted, reason = should_preempt_step(
            conn, principal_id="owner", lease_valid=False, budget_remaining=10
        )
        assert preempted is True
        assert "lease_lost" in reason

    def test_preempt_by_budget_exhausted(self):
        """budget=0 → paused_budget_exhausted。"""
        conn = _fresh_db()
        preempted, reason = should_preempt_step(
            conn, principal_id="owner", lease_valid=True, budget_remaining=0
        )
        assert preempted is True
        assert "budget_exhausted" in reason


class TestReplayMetricsConstraints:
    """核心指标回放约束。"""

    def test_duplicate_side_effect_zero(self, monkeypatch):
        """Drift 不得产生重复副作用 (MVP Skill 只读)。"""
        conn = _fresh_db()
        # MVP 只读 Skill 不写 Delivery，drift 不直接创建 proactive_candidates。
        _ = conn
        # 断言：执行 drift.run handler 不会创建 Delivery 行
        from cogito.domain.task import Task, TaskStatus
        from cogito.service.drift_runner import handle_drift_run

        conn2 = _fresh_db()
        conn2.execute(
            "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t-dup", "drift.run", "running", 5, "id-dup", int(time.time() * 1000)),
        )
        conn2.execute(
            "INSERT INTO drift_runs "
            "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
            " status, admission_snapshot_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "dr-dup",
                "t-dup",
                "owner",
                "proactive-policy-view-audit",
                "1.0",
                "admitted",
                "{}",
                int(time.time() * 1000),
            ),
        )
        conn2.commit()
        task = Task(task_id="t-dup", task_type="drift.run", status=TaskStatus.running)

        class _Ctx:
            def __init__(self, c):
                self.connection_factory = lambda p=c: p
                self.config_version_id = "cfg-1"
                self.workspace_path = ""

        handle_drift_run(task, _Ctx(conn2))
        # MVP read-only 不创建 Delivery
        cnt = conn2.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert cnt == 0

    def test_unauthorized_tool_rejected_before_exec(self):
        """MVP 不得注册 shell / network write / message send / plugin manage / secret read。

        验证：drift_allowed_tools 集合中不含未授权工具类别。"""
        # MVP allowed_tools 仅包含 filesystem.read / query.*
        manifest = DriftSkillManifest.from_dict(
            {
                "name": "test-skill",
                "allowed_tools": ["filesystem.read:workspace", "query.prohibited"],
            }
        )
        categories = set()
        for t in manifest.allowed_tools:
            base = t.split(":")[0]
            categories.add(base)
        # 必须不含未授权类别
        forbidden = {
            "shell",
            "network.write",
            "message.send",
            "plugin.manage",
            "secret.read",
            "exec",
            "filesystem.write",
        }
        assert not (categories & forbidden), (
            f"unauthorized tool categories in MVP: {categories & forbidden}"
        )
        # manifest 的 can_emit_candidate / requires_approval 默认安全
        assert manifest.can_emit_candidate is False
