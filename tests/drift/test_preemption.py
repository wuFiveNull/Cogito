"""M5: Drift 抢占、Checkpoint、恢复测试 (DR-P0-03)。

- 新 Turn 入站 → request_preemption → should_preempt_step 返回 True
- lease 无效 / budget 耗尽 / active_turn → preempt
- 安全点 write_checkpoint；恢复前版本校验
- config/skill 版本变化 → 校验不通过
"""

from __future__ import annotations

import sqlite3
import time
import json

import pytest

from cogito.domain.event import Event, EventClass, EventContext
from cogito.service.drift_preemption import (
    is_preemption_requested,
    request_preemption,
    should_preempt_step,
    validate_checkpoint_for_resume,
    write_checkpoint,
)
from cogito.store.migration import migrate


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


# ── preemption signal ──


class TestPreemptionSignal:
    def test_request_then_check(self, memory_db):
        request_preemption(memory_db, "owner", "new_turn")
        preempted, reason = is_preemption_requested(memory_db, "owner")
        assert preempted is True
        assert reason == "new_turn"

    def test_consumed_after_check(self, memory_db):
        request_preemption(memory_db, "owner", "new_turn")
        is_preemption_requested(memory_db, "owner")  # 消费
        preempted, _ = is_preemption_requested(memory_db, "owner")
        assert preempted is False

    def test_no_signal_no_preempt(self, memory_db):
        preempted, _ = is_preemption_requested(memory_db, "owner")
        assert preempted is False


# ── should_preempt_step 矩阵 ──


class TestShouldPreemptStep:
    def test_lease_invalid(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=False, budget_remaining=10
        )
        assert preempted is True
        assert "lease_lost" in reason

    def test_preemption_signal(self, memory_db):
        request_preemption(memory_db, "owner", "turn")
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=True, budget_remaining=10
        )
        assert preempted is True
        assert "preempted" in reason

    def test_active_turn(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db,
            principal_id="owner",
            lease_valid=True,
            budget_remaining=10,
            active_normal_turns=1,
        )
        assert preempted is True
        assert "active_turn" in reason

    def test_priority_backlog(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db,
            principal_id="owner",
            lease_valid=True,
            budget_remaining=10,
            priority_backlog=5,
        )
        assert preempted is True
        assert "priority_backlog" in reason

    def test_budget_exhausted(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=True, budget_remaining=0
        )
        assert preempted is True
        assert "budget_exhausted" in reason

    def test_all_clear(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=True, budget_remaining=5
        )
        assert preempted is False
        assert reason == ""


# ── checkpoint + resume validation ──


class TestCheckpoint:
    def test_write_returns_json_and_updates_drift_runs(self, memory_db):
        # 预备 drift_run 行 + TaskAttempt (真实 attempt_id)
        memory_db.execute(
            "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t-cp", "drift.run", "running", 5, "idemp-cp", int(time.time() * 1000)),
        )
        memory_db.execute(
            "INSERT INTO drift_runs "
            "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
            " status, admission_snapshot_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dr-cp", "t-cp", "owner", "s", "1.0", "running", "{}", int(time.time() * 1000)),
        )
        memory_db.execute(
            "INSERT INTO task_attempts "
            "(task_attempt_id, task_id, attempt_no, status, lease_owner, "
            " lease_version, lease_expires_at, started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "att-1",
                "t-cp",
                1,
                "running",
                "wkr",
                1,
                int(time.time() * 1000),
                int(time.time() * 1000),
            ),
        )
        memory_db.commit()
        ck_json = write_checkpoint(
            memory_db,
            drift_run_id="dr-cp",
            task_id="t-cp",
            attempt_id="att-1",
            skill_name="s",
            skill_version="1.0",
            step_index=3,
            cursor={"i": 5},
            completed_actions=["a", "b"],
            budget_used={"tool_calls": 2},
            config_version_id="cfg-1",
        )
        data = json.loads(ck_json)
        assert data["schema_version"] == 1
        assert data["step_index"] == 3
        assert data["cursor"] == {"i": 5}
        assert data["attempt_id"] == "att-1"
        # drift_runs.result_ref 指向 checkpoint
        row = memory_db.execute(
            "SELECT result_ref FROM drift_runs WHERE drift_run_id='dr-cp'"
        ).fetchone()
        assert row["result_ref"] == "drift-check:dr-cp:3"
        # P0-03 真实持久化验证：task_checkpoints 内嵌 JSON + hash
        ck_row = memory_db.execute(
            "SELECT payload_json, payload_hash "
            "FROM task_checkpoints WHERE task_id='t-cp' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert ck_row is not None, "必须写入 task_checkpoints 行"
        assert json.loads(ck_row["payload_json"])["step_index"] == 3
        from cogito.store.task_checkpoint_repo import _hash_json

        assert ck_row["payload_hash"] == _hash_json(ck_row["payload_json"])
        # 真实 attempt 被绑定：task_attempts.checkpoint_ref 指向该 checkpoint
        att_row = memory_db.execute(
            "SELECT checkpoint_ref FROM task_attempts WHERE task_attempt_id='att-1'"
        ).fetchone()
        assert att_row["checkpoint_ref"] == "drift-check:dr-cp:3"
        # tasks.checkpoint_ref 同步最新
        task_row = memory_db.execute(
            "SELECT checkpoint_ref FROM tasks WHERE task_id='t-cp'"
        ).fetchone()
        assert task_row["checkpoint_ref"] == "drift-check:dr-cp:3"

    def test_validate_compatible(self, memory_db):
        ck = json.dumps(
            {
                "schema_version": 1,
                "config_version_id": "cfg-1",
                "skill_version": "1.0",
                "step_index": 2,
                "cursor": {},
            }
        )
        ok, reason = validate_checkpoint_for_resume(
            ck, current_config_version_id="cfg-1", current_skill_version="1.0"
        )
        assert ok is True

    def test_validate_config_changed(self, memory_db):
        ck = json.dumps(
            {"schema_version": 1, "config_version_id": "cfg-old", "skill_version": "1.0"}
        )
        ok, reason = validate_checkpoint_for_resume(
            ck, current_config_version_id="cfg-new", current_skill_version="1.0"
        )
        assert ok is False
        assert "config_version" in reason

    def test_validate_skill_changed(self, memory_db):
        ck = json.dumps({"schema_version": 1, "config_version_id": "cfg-1", "skill_version": "1.0"})
        ok, _ = validate_checkpoint_for_resume(
            ck, current_config_version_id="cfg-1", current_skill_version="2.0"
        )
        assert ok is False

    def test_validate_schema_incompatible(self, memory_db):
        ck = json.dumps({"schema_version": 99})
        ok, _ = validate_checkpoint_for_resume(
            ck, current_config_version_id="cfg-1", current_skill_version="1.0"
        )
        assert ok is False


class TestShouldPreemptDynamicQueries:
    """PLAN-17 R4 P0-05: should_preempt_step 默认从 DB 动态查询 active turns /
    priority backlog，不再置 0 (fix the audit evidence default-0)。"""

    def test_preempts_on_active_turn_db(self, memory_db):
        """DB 有 running Turn 时应抢占，reason=active_turn。"""
        memory_db.execute(
            "INSERT INTO turns (turn_id,status,input_message_id,session_id,"
            " created_at) VALUES (?,?,?,?,?)",
            ("tr-1", "running", "m-1", "s-1", int(time.time() * 1000)),
        )
        memory_db.commit()
        preempted, reason = should_preempt_step(
            memory_db,
            principal_id="o",
            lease_valid=True,
            budget_remaining=8,
            active_normal_turns=None,
            priority_backlog=None,
        )
        assert preempted is True
        assert reason == "active_turn"

    def test_preempts_on_high_priority_backlog_db(self, memory_db):
        """DB 有 priority>=50 queued 任务应抢占，reason=priority_backlog。"""
        memory_db.execute(
            "INSERT INTO tasks (task_id,task_type,status,priority,"
            " idempotency_key, created_at) VALUES (?,?,?,?,?,?)",
            ("tk-high", "connector.poll", "queued", 80, "id-hi", int(time.time() * 1000)),
        )
        memory_db.commit()
        preempted, reason = should_preempt_step(
            memory_db,
            principal_id="o",
            lease_valid=True,
            budget_remaining=8,
            active_normal_turns=0,
            priority_backlog=None,
        )
        assert preempted is True
        assert reason == "priority_backlog"

    def test_no_preempt_when_db_empty(self, memory_db):
        """DB 无 active turns + 无 backlog + lease valid + budget > 0 → 不抢占。"""
        preempted, reason = should_preempt_step(
            memory_db,
            principal_id="o",
            lease_valid=True,
            budget_remaining=8,
            active_normal_turns=None,
            priority_backlog=None,
        )
        assert preempted is False
        assert reason == ""

    def test_explicit_arguments_still_honored(self, memory_db):
        """显式传入非 None 值时覆盖 DB 查询 (测试/特殊场景仍可用)。"""
        preempted, reason = should_preempt_step(
            memory_db,
            principal_id="o",
            lease_valid=True,
            budget_remaining=8,
            active_normal_turns=3,
            priority_backlog=0,
        )
        assert preempted is True
        assert reason == "active_turn"
