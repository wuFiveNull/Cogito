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
            memory_db, principal_id="owner", lease_valid=False, budget_remaining=10)
        assert preempted is True
        assert "lease_lost" in reason

    def test_preemption_signal(self, memory_db):
        request_preemption(memory_db, "owner", "turn")
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=True, budget_remaining=10)
        assert preempted is True
        assert "preempted" in reason

    def test_active_turn(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=True,
            budget_remaining=10, active_normal_turns=1)
        assert preempted is True
        assert "active_turn" in reason

    def test_priority_backlog(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=True,
            budget_remaining=10, priority_backlog=5)
        assert preempted is True
        assert "priority_backlog" in reason

    def test_budget_exhausted(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=True, budget_remaining=0)
        assert preempted is True
        assert "budget_exhausted" in reason

    def test_all_clear(self, memory_db):
        preempted, reason = should_preempt_step(
            memory_db, principal_id="owner", lease_valid=True, budget_remaining=5)
        assert preempted is False
        assert reason == ""


# ── checkpoint + resume validation ──


class TestCheckpoint:
    def test_write_returns_json_and_updates_drift_runs(self, memory_db):
        # 预备 drift_run 行
        memory_db.execute(
            "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t-cp", "drift.run", "running", 5, "idemp-cp", int(time.time()*1000)),
        )
        memory_db.execute(
            "INSERT INTO drift_runs "
            "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
            " status, admission_snapshot_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dr-cp", "t-cp", "owner", "s", "1.0", "running",
             "{}", int(time.time()*1000)),
        )
        memory_db.commit()
        ck_json = write_checkpoint(
            memory_db, drift_run_id="dr-cp", task_id="t-cp",
            attempt_id="att-1", skill_name="s", skill_version="1.0",
            step_index=3, cursor={"i": 5}, completed_actions=["a", "b"],
            budget_used={"tool_calls": 2}, config_version_id="cfg-1")
        data = json.loads(ck_json)
        assert data["schema_version"] == 1
        assert data["step_index"] == 3
        assert data["cursor"] == {"i": 5}
        row = memory_db.execute(
            "SELECT result_ref FROM drift_runs WHERE drift_run_id='dr-cp'"
        ).fetchone()
        assert row["result_ref"] == "drift-check:dr-cp:3"

    def test_validate_compatible(self, memory_db):
        ck = json.dumps({
            "schema_version": 1, "config_version_id": "cfg-1",
            "skill_version": "1.0", "step_index": 2, "cursor": {}})
        ok, reason = validate_checkpoint_for_resume(
            ck, current_config_version_id="cfg-1", current_skill_version="1.0")
        assert ok is True

    def test_validate_config_changed(self, memory_db):
        ck = json.dumps({
            "schema_version": 1, "config_version_id": "cfg-old",
            "skill_version": "1.0"})
        ok, reason = validate_checkpoint_for_resume(
            ck, current_config_version_id="cfg-new", current_skill_version="1.0")
        assert ok is False
        assert "config_version" in reason

    def test_validate_skill_changed(self, memory_db):
        ck = json.dumps({
            "schema_version": 1, "config_version_id": "cfg-1",
            "skill_version": "1.0"})
        ok, _ = validate_checkpoint_for_resume(
            ck, current_config_version_id="cfg-1", current_skill_version="2.0")
        assert ok is False

    def test_validate_schema_incompatible(self, memory_db):
        ck = json.dumps({"schema_version": 99})
        ok, _ = validate_checkpoint_for_resume(
            ck, current_config_version_id="cfg-1", current_skill_version="1.0")
        assert ok is False
