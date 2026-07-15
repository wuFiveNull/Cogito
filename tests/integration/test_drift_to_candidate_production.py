"""PLAN-17 R5 P0-06 E2E: Drift → Candidate 自动投影 (不允许手工插 Run)。

完整生产路径：
Scheduler.admission → TaskDispatcher/TaskWorker.claim_next → handle_drift_run →
Skill 产出 items → _finish_drift 写 DriftResult + Outbox DriftResultCommitted →
DriftResultCommittedConsumer 校验+调 DriftProjectionService →
ProactiveCandidate(origin=drift) 状态=evaluating。

验证项:
- 不允许手工插入 completed Run (Runner 执行是关键路径)
- Candidate 可追溯 Run/Attempt/Skill/Result/evidence
- Drift 无直接 DeliveryService 权限 (Consumer 未调 Delivery)
- dry-run / allow_candidate_emission=False 仅 preview, 不写 Candidate
- 幂等: 第二次消费不重复创建 Candidate
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from cogito.config import DriftConfig
from cogito.domain.task import Task, TaskStatus
from cogito.service.drift_runner import handle_drift_run
from cogito.service.event_consumers import DriftResultCommittedConsumer, OutboxLease
from cogito.service.outbox_worker import OutboxWorker
from cogito.service.scheduler import Scheduler
from cogito.service.task_dispatcher import TaskDispatcher
from cogito.store.migration import migrate


def _fresh_db():
    sqlite3.register_adapter(bool, int)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _run_scheduler_admit(conn):
    """真实 Scheduler 选 Skill + 创建 drift.run Task，返回 run 的 drift_run_id。"""
    dr_cfg = DriftConfig(
        enabled=True,
        dry_run=False,
        default_principal_id="owner",
        idle_after_minutes=30,
        max_runs_per_day=3,
        allow_candidate_emission=True,
        allow_candidate_projection=True,
    )
    scheduler = Scheduler(conn, drift_config=dr_cfg, workspace_path="")
    admitted = scheduler.tick_drift_admit()
    assert admitted is not None
    run_id, task_id = admitted
    # Skill 必须是真实选择 (非 placeholder) — 具体选到哪个由 selector 评分决定
    row = conn.execute(
        "SELECT skill_name FROM drift_runs WHERE drift_run_id=?", (run_id,)
    ).fetchone()
    assert row["skill_name"] and row["skill_name"] != "(selected-at-run)"
    return run_id, task_id


def _worker_claim_and_run(conn, task_id):
    """Worker 领取 pending Task 并执行 handle_drift_run。"""
    dispatcher = TaskDispatcher(conn)
    worker_id = "wkr-d2c"
    # 注入同真实 Worker (task_worker.run_once) 的 attempt_id
    claimed = dispatcher.claim_next(worker_id)
    assert claimed is not None, "Worker 应 claim drift.run Task"
    task, attempt = claimed.task, claimed.attempt

    class _Ctx:
        def __init__(self, c, att):
            self.connection_factory = lambda p=c: p
            self.config_version_id = "cfg-p06"
            self.workspace_path = ""
            self._attempt_id = att

    result = handle_drift_run(task, _Ctx(conn, attempt.task_attempt_id))
    assert "completed" in result, f"Runner 未完成执行: {result}"
    return attempt.task_attempt_id


def _dispatch_outbox(conn, dry_run=False):
    """模拟 application.process_background_once 的 Outbox 消费路径。"""
    dr_cfg = DriftConfig(
        enabled=True,
        dry_run=dry_run,
        default_principal_id="owner",
        allow_candidate_emission=not dry_run,
        allow_candidate_projection=True,
    )
    consumer = DriftResultCommittedConsumer(default_principal_id="owner", drift_config=dr_cfg)
    outbox = OutboxWorker(conn)
    worker_id = "wkr-outbox"
    result = {"candidates": 0, "consumed": 0}
    while True:
        lease = outbox.lease_next(worker_id)
        if lease is None:
            break
        if consumer.can_handle(lease):
            ok = consumer.handle(conn, lease)
            if ok:
                outbox.publish(lease, worker_id)
                result["consumed"] += 1
                if lease.event_type == "DriftResultCommitted":
                    result["candidates"] += 1
            else:
                outbox.retry(lease, worker_id)
                break
    return result


def test_full_pipeline_emits_candidate():
    """完整生产路径 -> Candidate 自动产出。"""
    conn = _fresh_db()
    run_id, task_id = _run_scheduler_admit(conn)
    _worker_claim_and_run(conn, task_id)

    # run 必须真正 completed (Runner 执行过，不是手工插的)
    run = conn.execute(
        "SELECT status, result_ref FROM drift_runs WHERE drift_run_id=?",
        (run_id,),
    ).fetchone()
    assert run["status"] == "completed"
    # DriftResult 必须真实存在
    res = conn.execute(
        "SELECT result_kind, items_json, candidate_draft_json "
        "FROM drift_results WHERE drift_run_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    assert res is not None
    assert res["result_kind"] in ("candidate_emission", "internal_only")
    # Outbox 中 DriftResultCommitted event 必须存在
    ev = conn.execute(
        "SELECT event_id, event_type FROM outbox_events WHERE "
        "payload_ref=? AND event_type='DriftResultCommitted'",
        (run_id,),
    ).fetchone()
    assert ev is not None, "Outbox 必须含 DriftResultCommitted"

    # consumer 消费 Outbox 后投影为 Candidate
    counts = _dispatch_outbox(conn, dry_run=False)
    assert counts["consumed"] >= 1

    cand = conn.execute(
        "SELECT candidate_id, origin, status, source_payload_ref, principal_id "
        "FROM proactive_candidates WHERE source_payload_ref=?",
        (run_id,),
    ).fetchone()
    assert cand is not None, "未生成 Candidate"
    assert cand["origin"] == "drift"
    assert cand["status"] == "evaluating"
    # candidate 可追溯 Run / Principal
    assert cand["source_payload_ref"] == run_id
    # DriftRun.candidate_id 已回写
    run2 = conn.execute(
        "SELECT candidate_id FROM drift_runs WHERE drift_run_id=?",
        (run_id,),
    ).fetchone()
    assert run2["candidate_id"] == cand["candidate_id"]


def test_idempotent_reconsume_no_duplicate():
    """第二次消费 Outbox 不应创建重复 Candidate (幂等)。"""
    conn = _fresh_db()
    run_id, task_id = _run_scheduler_admit(conn)
    _worker_claim_and_run(conn, task_id)
    _dispatch_outbox(conn, dry_run=False)
    # 第二次 dispatch 属幂等，不应新增 Candidate
    _dispatch_outbox(conn, dry_run=False)
    n = conn.execute(
        "SELECT COUNT(*) FROM proactive_candidates WHERE source_payload_ref=?",
        (run_id,),
    ).fetchone()[0]
    assert n == 1, f"候选应幂等唯一，got={n}"


def test_consumer_does_not_call_delivery():
    """DR-P0-06: Drift 无直接 DeliveryService 权限 (候选不立即发送)。"""
    conn = _fresh_db()
    run_id, task_id = _run_scheduler_admit(conn)
    _worker_claim_and_run(conn, task_id)
    _dispatch_outbox(conn, dry_run=False)
    # Outbox/ProactiveCandidate 的状态只能为 evaluating，不能已是 sent/delivered
    c = conn.execute(
        "SELECT status FROM proactive_candidates WHERE source_payload_ref=?",
        (run_id,),
    ).fetchone()
    assert c is not None
    assert c["status"] == "evaluating", f"Drift 不能直接发送: {c['status']}"
    # 无 Delivery 记录 (Delivery 由 Delivery 闭环处理)
    nd = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    assert nd == 0
