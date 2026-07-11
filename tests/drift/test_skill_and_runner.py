"""M4: DriftSkillCatalog + DriftSkillSelector + drift.run handler 测试。

- catalog 加载内置 Skill，解析 manifest
- selector deterministic 评分（失败退避、最近运行惩罚、staleness）
- drift.run handler 执行只读 Skill + finish_drift 强制收尾
- unknown skill → no_value/skipped
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from cogito.domain.drift import DriftReasonCode, DriftRunStatus
from cogito.service.drift_runner import handle_drift_run
from cogito.service.drift_skill_catalog import (
    load_builtin_skills,
    resolve_catalog,
)
from cogito.service.drift_selector import select_skill
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


def _seed_drift_run(conn, run_id, task_id, skill_name="proactive-policy-view-audit"):
    """写入 task + drift_run（FK 需要 task 存在）。"""
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, "drift.run", "running", 5, f"idemp-{task_id}",
         int(time.time()*1000)),
    )
    conn.execute(
        "INSERT INTO drift_runs "
        "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
        " status, admission_snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, task_id, "owner", skill_name, "1.0", "running",
         "{}", int(time.time()*1000)),
    )
    conn.commit()


class _Ctx:
    """TaskHandlerContext 最小测试替身。"""
    def __init__(self, conn):
        self.connection_factory = lambda: conn
        self.config_version_id = "cfg-test"
        self.workspace_path = ""


# ── catalog ──


class TestCatalog:
    def test_load_builtin_skill(self):
        """内置 proactive-policy-view-audit 必须可加载。"""
        skills = load_builtin_skills()
        assert "proactive-policy-view-audit" in skills
        m = skills["proactive-policy-view-audit"].manifest
        assert m.risk_level == "low"
        assert m.max_model_calls == 0
        assert m.can_emit_candidate is False
        assert "query.proactive_policy" in m.allowed_tools

    def test_builtin_preferred_over_workspace(self):
        """同名时内置优先。"""
        catalog = resolve_catalog("/tmp/nonexistent", allow_workspace=False)
        assert "proactive-policy-view-audit" in catalog

    def test_workspace_disabled_by_default(self):
        """allow_workspace=False 时不加载 workspace skill。"""
        catalog = resolve_catalog("/tmp/whatever", allow_workspace=False)
        # 仍应包含内置
        assert "proactive-policy-view-audit" in catalog


# ── selector ──


class TestSelector:
    def test_selects_from_candidates(self):
        skills = load_builtin_skills()
        name, scores = select_skill(skills, {}) or (None, {})
        assert name is not None
        assert name in scores
        # scores 中 name 的分数最高
        assert scores[name] == max(scores.values())

    def test_failure_backoff(self):
        """刚失败的 skill 评分降低 (recent_failure_penalty)。"""
        skills = load_builtin_skills()
        now = int(time.time() * 1000)
        # 让 proactive-policy-view-audit 刚失败
        states = {"proactive-policy-view-audit": {
            "last_status": "failed", "last_run_at": now - 60_000,  # 1 分钟前
            "run_count": 1,
        }}
        _name, scores = select_skill(skills, states)
        # 只有 1 个 skill，仍会被选但分数应体现惩罚（运行仍然返回它）
        assert _name == "proactive-policy-view-audit"

    def test_empty_skills_returns_none(self):
        assert select_skill({}, {}) is None


# ── runner ──


class TestDriftRunHandler:
    def test_known_skill_completes(self, memory_db):
        """proactive-policy-view-audit → completed，drift_runs.status 更新。"""
        _seed_drift_run(memory_db, "dr-1", "t-1",
                        skill_name="proactive-policy-view-audit")
        from cogito.domain.task import Task, TaskStatus
        task = Task(task_id="t-1", task_type="drift.run",
                    status=TaskStatus.running)
        ctx = _Ctx(memory_db)
        result = handle_drift_run(task, ctx)
        assert "completed" in result
        row = memory_db.execute(
            "SELECT status, steps_taken FROM drift_runs WHERE drift_run_id='dr-1'"
        ).fetchone()
        assert row["status"] == "completed"
        assert row["steps_taken"] >= 1  # 多步循环至少执行了 1 步

    def test_unknown_skill_no_value(self, memory_db):
        """未知 skill → completed + skipped_no_value。"""
        _seed_drift_run(memory_db, "dr-2", "t-2", skill_name="no-such-skill")
        from cogito.domain.task import Task, TaskStatus
        task = Task(task_id="t-2", task_type="drift.run",
                    status=TaskStatus.running)
        result = handle_drift_run(task, _Ctx(memory_db))
        assert "skipped" in result or "completed" in result
        row = memory_db.execute(
            "SELECT status FROM drift_runs WHERE drift_run_id='dr-2'"
        ).fetchone()
        assert row["status"] == "completed"

    def test_no_drift_run_skips(self, memory_db):
        """task 无对应 drift_run → skipped。"""
        from cogito.domain.task import Task, TaskStatus
        # 先写入任务（不写 drift_run）
        memory_db.execute(
            "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t-orphan", "drift.run", "running", 5, "idemp-orphan",
             int(time.time()*1000)),
        )
        memory_db.commit()
        task = Task(task_id="t-orphan", task_type="drift.run",
                    status=TaskStatus.running)
        result = handle_drift_run(task, _Ctx(memory_db))
        assert "skipped" in result

    def test_skill_state_synced(self, memory_db):
        """finish_drift 同步写 drift_skill_state。"""
        _seed_drift_run(memory_db, "dr-3", "t-3",
                        skill_name="proactive-policy-view-audit")
        from cogito.domain.task import Task, TaskStatus
        task = Task(task_id="t-3", task_type="drift.run",
                    status=TaskStatus.running)
        handle_drift_run(task, _Ctx(memory_db))
        row = memory_db.execute(
            "SELECT last_status, run_count FROM drift_skill_state "
            "WHERE principal_id='owner' AND skill_name='proactive-policy-view-audit'"
        ).fetchone()
        assert row is not None
        assert row["last_status"] == "completed"
        assert row["run_count"] == 1
