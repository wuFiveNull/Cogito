"""Drift 抢占与恢复 (M5 / DR-P0-03)。

每步执行前检查：lease_valid / cancel-preempt_requested / active_normal_turns /
priority_backlog / budget_remaining。新 Turn 入站后发出 preemption signal，
Drift 在安全点写 DriftCheckpointV1 + 更新 TaskAttempt.checkpoint_ref + 释放 Lease。

恢复前校验 config_version_id / skill_version / checkpoint_schema_version。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from cogito.domain.drift import (
    DriftCheckpointV1,
    DriftReasonCode,
)
from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_projection_store import EventProjectionStore
from cogito.store.event_store import EventStore

_LOGGER = logging.getLogger(__name__)

# preemption signal 表由 migration 0049 创建；此处不再重复建表。


def request_preemption(conn, principal_id: str, reason: str) -> None:
    """新 Turn 入站后调用：置位 preemption signal via Event。"""
    EventStore(conn).append(
        Event(
            event_type="drift.preemption.requested",
            stream_type="drift_preemption",
            stream_id=principal_id,
            producer="drift-preemption",
            event_class=EventClass.OPERATION,
            summary=f"Drift preemption requested: {reason}",
            attributes={"reason": reason},
            outcome="requested",
            idempotency_key=f"drift:preemption:{principal_id}:{int(time.time() * 1000)}",
        ),
    )
    conn.commit()


def is_preemption_requested(conn, principal_id: str) -> tuple[bool, str]:
    """检查并清除 preemption signal via Event。"""
    events = EventStore(conn).read_stream("drift_preemption", principal_id)
    if events:
        latest = events[-1]
        if latest.event_type == "drift.preemption.requested":
            reason = str(latest.attributes.get("reason", ""))
            # Mark as consumed
            EventStore(conn).append(
                Event(
                    event_type="drift.preemption.consumed",
                    stream_type="drift_preemption",
                    stream_id=principal_id,
                    producer="drift-preemption",
                    event_class=EventClass.OPERATION,
                    summary="Drift preemption consumed",
                    idempotency_key=f"drift:preemption:consumed:{principal_id}:{latest.occurred_at}",
                ),
                expected_version=latest.stream_version,
            )
            conn.commit()
            return True, reason
    return False, ""


def _count_active_normal_turns(conn) -> int:
    """从 Event projection 查询正在运行的 normal turns。"""
    projections = EventProjectionStore(EventStore(conn))
    return len([
        t for t in projections.turns()
        if t["status"] in ("running", "accepted", "queued")
    ])


def _count_high_priority_backlog(conn, threshold: int = 50) -> int:
    """从 Event projection 查询高优先级任务积压。"""
    projections = EventProjectionStore(EventStore(conn))
    return len([
        t for t in projections.tasks()
        if t["status"] in ("queued", "scheduled", "running")
        and (t["priority"] or 0) >= threshold
    ])


def should_preempt_step(
    conn,
    *,
    principal_id: str,
    lease_valid: bool,
    budget_remaining: int,
    active_normal_turns: int | None = None,
    priority_backlog: int | None = None,
    high_priority_threshold: int = 50,
) -> tuple[bool, str]:
    """Drift 单步前检查。返回 (should_preempt, reason)。

    active_normal_turns / priority_backlog 为 None 时从 DB 动态查询
    (修复 P0-05 的默认 0 使 turn 到达永不被抢占的证据)。
    """
    if not lease_valid:
        return True, DriftReasonCode.lease_lost
    preempted, reason = is_preemption_requested(conn, principal_id)
    if preempted:
        return True, DriftReasonCode.preempted_by_turn
    if active_normal_turns is None:
        active_normal_turns = _count_active_normal_turns(conn)
    if active_normal_turns > 0:
        return True, DriftReasonCode.active_turn
    if priority_backlog is None:
        priority_backlog = _count_high_priority_backlog(conn, threshold=high_priority_threshold)
    if priority_backlog > 0:
        return True, DriftReasonCode.priority_backlog
    if budget_remaining <= 0:
        return True, DriftReasonCode.paused_budget_exhausted
    return False, ""


def write_checkpoint(
    conn,
    *,
    drift_run_id: str,
    task_id: str,
    attempt_id: str,
    skill_name: str,
    skill_version: str,
    step_index: int,
    cursor: dict[str, Any],
    completed_actions: list[str],
    budget_used: dict[str, int],
    config_version_id: str,
    capability_snapshot_version: str = "",
    checkpoint_type: str = "drift-step",
    payload_store: Any = None,
) -> str:
    """写 DriftCheckpointV1 真实持久化 (PLAN-17 R3 P0-03/04)。

    写入顺序（同一事务 commit）：
    1. JSON 主体落受限 PayloadStore，并追加版本化 Checkpoint Event；
    2. 刷新 tasks.checkpoint_ref / task_attempts.checkpoint_ref；
    3. 刷新 drift_skill_state.checkpoint_ref / cursor_json；
    4. 更新 drift_runs.result_ref 指向该 checkpoint。

    attempt_id 必须真实（不为空），否则无法绑定 Attempt。
    """
    # P0-04: 放宽到容忍缺省 attempt：当调用方实在拿不到 attempt_id 时回退到按
    # task 最新 running attempt 解析，绝不静默丢弃。
    real_attempt_id = attempt_id.strip() if attempt_id else ""
    if not real_attempt_id:
        # Fallback: find latest running attempt from Event stream
        from cogito.store.event_replay import replay_task_attempt

        grouped: dict[str, list[Event]] = {}
        for event in EventStore(conn).read_stream_type("task_attempt"):
            if event.context.task_id == task_id:
                grouped.setdefault(event.stream_id, []).append(event)
        best = None
        for aid, stream in grouped.items():
            proj = replay_task_attempt(stream, aid)
            if proj is not None and proj.status in {"running", "created"}:
                if best is None or proj.attempt_no > best.attempt_no:
                    best = proj
        if best is not None:
            real_attempt_id = best.task_attempt_id

    ck = DriftCheckpointV1(
        drift_run_id=drift_run_id,
        task_id=task_id,
        attempt_id=real_attempt_id,
        skill_name=skill_name,
        skill_version=skill_version,
        step_index=step_index,
        cursor=dict(cursor),
        completed_actions=list(completed_actions),
        budget_used=dict(budget_used),
        config_version_id=config_version_id,
        capability_snapshot_version=capability_snapshot_version,
    )
    data = ck.to_dict()
    payload_json = json.dumps(data, ensure_ascii=False)
    ref = f"drift-check:{drift_run_id}:{step_index}"

    now = int(time.time() * 1000)
    from cogito.store.task_checkpoint_repo import (
        TaskCheckpoint,
        TaskCheckpointRepository,
        _hash_json,
    )

    ck_id = f"ck-{uuid4_hex()[:16]}"
    stored_checkpoint = TaskCheckpointRepository(conn, payload_store=payload_store).insert(
        TaskCheckpoint(
            checkpoint_id=ck_id,
            task_id=task_id,
            task_attempt_id=real_attempt_id,
            drift_run_id=drift_run_id,
            checkpoint_type=checkpoint_type,
            schema_version=1,
            payload_ref=ref,
            payload_json=payload_json,
            payload_hash=_hash_json(payload_json),
            config_version_id=config_version_id,
            capability_snapshot_version=capability_snapshot_version,
            created_at=now,
        )
    )

    from cogito.store.drift_repo import DriftRunRepository, DriftSkillStateRepository
    from cogito.store.event_store import EventStore

    event_sourced = bool(EventStore(conn).read_stream_type("drift_run", limit=1))
    drift_repo = DriftRunRepository(conn, event_sourced=event_sourced)
    if event_sourced:
        drift_repo.record_checkpoint(
            drift_run_id,
            stored_checkpoint.payload_ref,
            stored_checkpoint.payload_hash,
        )
        run = drift_repo.get(drift_run_id)
        if run is not None:
            DriftSkillStateRepository(conn, event_sourced=True).upsert(
                principal_id=str(run.get("principal_id") or ""),
                skill_name=skill_name,
                skill_version=skill_version,
                checkpoint_ref=stored_checkpoint.payload_ref,
            )
    else:
        conn.execute("UPDATE tasks SET checkpoint_ref=? WHERE task_id=?", (ref, task_id))
        if real_attempt_id:
            conn.execute(
                "UPDATE task_attempts SET checkpoint_ref=? WHERE task_attempt_id=?",
                (ref, real_attempt_id),
            )
        prow = conn.execute(
            "SELECT principal_id FROM drift_runs WHERE drift_run_id=?",
            (drift_run_id,),
        ).fetchone()
        if prow is not None:
            conn.execute(
                "UPDATE drift_skill_state "
                "SET checkpoint_ref=?, cursor_json=?, updated_at=? "
                "WHERE principal_id=? AND skill_name=? AND skill_version=?",
                (ref, json.dumps(dict(cursor), ensure_ascii=False), now, prow[0], skill_name, skill_version),
            )
        drift_repo.record_checkpoint(drift_run_id, ref)
    conn.commit()
    return payload_json


def uuid4_hex() -> str:
    import uuid as _uuid

    return _uuid.uuid4().hex


def validate_checkpoint_for_resume(
    checkpoint_json: str,
    *,
    current_config_version_id: str,
    current_skill_version: str,
) -> tuple[bool, str]:
    """恢复前校验：config/skill/checkpoint schema 版本兼容。

    不兼容 → (False, reason)。
    """
    try:
        data = json.loads(checkpoint_json)
    except Exception:
        return False, "invalid checkpoint json"
    schema = data.get("schema_version")
    if schema != 1:
        return False, f"incompatible checkpoint schema_version={schema}"
    if data.get("config_version_id") and data["config_version_id"] != current_config_version_id:
        return False, "config_version changed"
    if data.get("skill_version") and data["skill_version"] != current_skill_version:
        return False, "skill_version changed"
    return True, ""
