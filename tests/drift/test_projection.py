"""R9 M6: Drift 结果走 Candidate 投影 + Dashboard + 反馈测试。

- DriftProjectionService.project：completed run → ProactiveCandidate(origin=drift)
- 未完成 run / 已投影 / principal 不匹配 → 不创建
- dry_run 仅 preview
- 同一 DriftRun 最多一个用户可见 Candidate
- Dashboard 端点返回真实值（不再是 None 占位）
- 反馈 → 分级 ACK 窗口
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from cogito.domain.drift import DriftCandidateDraft, DriftRunStatus
from cogito.service.drift_projection import DriftProjectionService
from cogito.service.proactive_feedback import (
    ack_window_for,
    record_feedback,
)
from cogito.store.migration import migrate
from cogito.store.proactive_repo import ProactiveCandidateRepository


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


def _seed_run(conn, run_id, status="completed", principal_id="owner"):
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"t-{run_id}", "drift.run", "running", 5, f"idem-{run_id}", int(time.time() * 1000)),
    )
    conn.execute(
        "INSERT INTO drift_runs "
        "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
        " status, admission_snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, f"t-{run_id}", principal_id, "s", "1.0", status, "{}", int(time.time() * 1000)),
    )
    conn.commit()


def _draft() -> DriftCandidateDraft:
    return DriftCandidateDraft(
        topic="drift.audit",
        summary="proactive policy view: v1 dry_run=True",
        evidence_refs=("dr-1",),
        trust_label="system_generated",
        urgency=0.6,
        confidence=0.8,
        relevance=0.7,
    )


# ── projection ──


class TestProjection:
    def test_completes_run_projects_to_candidate(self, memory_db):
        _seed_run(memory_db, "dr-1", status="completed")
        svc = DriftProjectionService(memory_db, dry_run=False)
        cid = svc.project(drift_run_id="dr-1", draft=_draft(), principal_id="owner")
        assert cid is not None
        assert cid.startswith("pc-drift-")
        # origin=drift
        row = memory_db.execute(
            "SELECT origin, stream_type, topic FROM proactive_candidates WHERE candidate_id=?",
            (cid,),
        ).fetchone()
        assert row["origin"] == "drift"
        assert row["stream_type"] == "context"
        assert row["topic"] == "drift.audit"

    def test_incomplete_run_not_projected(self, memory_db):
        _seed_run(memory_db, "dr-2", status="running")
        svc = DriftProjectionService(memory_db, dry_run=False)
        assert svc.project(drift_run_id="dr-2", draft=_draft()) is None

    def test_principal_mismatch_rejected(self, memory_db):
        _seed_run(memory_db, "dr-3", status="completed", principal_id="owner")
        svc = DriftProjectionService(memory_db, dry_run=False)
        assert svc.project(drift_run_id="dr-3", draft=_draft(), principal_id="other") is None

    def test_same_run_at_most_one_candidate(self, memory_db):
        _seed_run(memory_db, "dr-4", status="completed")
        svc = DriftProjectionService(memory_db, dry_run=False)
        first = svc.project(drift_run_id="dr-4", draft=_draft())
        second = svc.project(drift_run_id="dr-4", draft=_draft())
        assert first is not None
        assert second is None  # 重复投影被拒

    def test_dry_run_preview_only(self, memory_db):
        _seed_run(memory_db, "dr-5", status="completed")
        svc = DriftProjectionService(memory_db, dry_run=True)
        result = svc.project(drift_run_id="dr-5", draft=_draft())
        assert result is None
        # 未创建 Candidate
        cnt = memory_db.execute(
            "SELECT COUNT(*) FROM proactive_candidates WHERE origin='dr-5'"
        ).fetchone()[0]
        assert cnt == 0

    def test_candidate_traces_to_drift_run(self, memory_db):
        _seed_run(memory_db, "dr-6", status="completed")
        svc = DriftProjectionService(memory_db, dry_run=False)
        cid = svc.project(drift_run_id="dr-6", draft=_draft())
        cand = ProactiveCandidateRepository(memory_db).get(cid)
        assert cand is not None
        assert cand.origin == "drift"
        assert cand.source_payload_ref == "dr-6"  # 可追溯


# ── Dashboard ──


class TestDriftDashboard:
    def test_status_returns_real_preemption_reason(self, memory_db):
        _seed_run(memory_db, "dr-7", status="paused")
        # 写一个真实的 preemption_reason
        memory_db.execute(
            "UPDATE drift_runs SET preemption_reason=? WHERE drift_run_id='dr-7'",
            ("preempted_by_turn",),
        )
        memory_db.commit()
        from cogito.service.api.query_service import SqliteQueryService

        # 用 minimal config 构造
        from cogito.config import Config

        svc = SqliteQueryService(memory_db, Config())
        status = svc.drift_status(principal_id="owner")
        assert status["latest_preemption_reason"] == "preempted_by_turn"  # 真实值，不再是 None
        assert status["total_runs"] == 1

    def test_no_runs_returns_none_reason(self, memory_db):
        from cogito.service.api.query_service import SqliteQueryService
        from cogito.config import Config

        svc = SqliteQueryService(memory_db, Config())
        status = svc.drift_status(principal_id="owner")
        assert status["latest_preemption_reason"] is None
        assert status["total_runs"] == 0

    def test_list_drift_runs_returns_real_rows(self, memory_db):
        _seed_run(memory_db, "dr-8", status="completed")
        from cogito.service.api.query_service import SqliteQueryService
        from cogito.config import Config

        svc = SqliteQueryService(memory_db, Config())
        runs = svc.list_drift_runs(principal_id="owner", limit=10)
        assert len(runs) == 1
        assert runs[0]["drift_run_id"] == "dr-8"


# ── feedback ──


class TestFeedback:
    def test_ack_windows(self):
        assert ack_window_for("accepted", "send_now") > ack_window_for("dismissed")
        assert ack_window_for("duplicate") > ack_window_for("accepted")
        assert ack_window_for("alert_consumed") == 0

    def test_record_feedback_returns_window(self, memory_db):
        _seed_run(memory_db, "dr-fb", status="completed")
        # 给一个 send_now decision
        memory_db.execute(
            "INSERT INTO proactive_candidates "
            "(candidate_id, principal_id, stream_type, topic, novelty, relevance, "
            " urgency, confidence, recommended_action, policy_version, "
            " idempotency_key, source_event_ids_json, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c-fb",
                "owner",
                "context",
                "t",
                0.6,
                0.7,
                0.6,
                0.8,
                "evaluate",
                1,
                "k-fb",
                "[]",
                int(time.time() * 1000),
                "evaluating",
            ),
        )
        memory_db.execute(
            "INSERT INTO proactive_decisions_v2 "
            "(decision_id, candidate_id, principal_id, action, rule_results_json, "
            " policy_version, dry_run, decided_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dec-fb", "c-fb", "owner", "send_now", "{}", 1, 0, int(time.time() * 1000)),
        )
        memory_db.commit()
        result = record_feedback(
            memory_db, event_type="accepted", candidate_id="c-fb", principal_id="owner"
        )
        assert result["recorded"] is True
        assert result["ack_window_seconds"] > 0
        assert result["action"] == "send_now"
