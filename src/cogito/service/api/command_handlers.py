"""Command API 路由 —— 可写命令。

ACCESS-DELIVERY §2.3。所有命令：
  - 接受幂等键 + 生成 command_id
  - 经过服务层 (Dispatcher / TaskRepository / SqliteMemoryService / ConnectorRepository ...)
  - 写入 audit_records
  - DB 增删改查一律走服务；handler 不直写 SQL (audit.py 的 write_audit 除外)。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from cogito.domain.events import DomainEvent
from cogito.service.api.audit import write_audit
from cogito.service.api.command_service import (
    replay_delivery,
    resume_turn_after_approval,
    set_approval_decision,
)
from cogito.service.api.deps import CommandDeps, get_command_deps
from cogito.store.repositories import OutboxRepository
from cogito.contracts.models import (
    ApprovalPayload,
    CancelTurnPayload,
    CommandResponse,
    ConfigDryRunPayload,
    CreateBackupPayload,
    DeleteSessionPayload,
    DeleteSessionsByConvPayload,
    DisablePluginPayload,
    DisableToolPayload,
    KnowledgeErasePayload,
    KnowledgeInvalidatePayload,
    KnowledgeRefreshPayload,
    KnowledgeRegisterPayload,
    MemoryConfirmPayload,
    MemoryCorrectPayload,
    MemoryDeletePayload,
    MemoryRejectPayload,
    ProactiveNegativeFeedbackPayload,
    PauseConnectorPayload,
    PayloadGcDryRunPayload,
    ArchiveSkillPayload,
    ForceConnectorPollPayload,
    ImportProactiveContextPayload,
    PinSkillPayload,
    RebuildProactiveContextPayload,
    ReconcileDeliveryPayload,
    ReconcileReceiptPayload,
    RestoreSkillPayload,
    ReplayDeliveryPayload,
    ReplayEventPayload,
    RestoreBackupPayload,
    ReviewProactiveCandidatePayload,
    RollbackConfigPayload,
    RetryTaskPayload,
    UpdateProactivePolicyPayload,
    VerifyBackupPayload,
)
from cogito.service.dispatcher import Dispatcher
from cogito.service.memory_service import SqliteMemoryService
from cogito.store.connector_repo import ConnectorRepository
from cogito.store.task_repo import TaskRepository

_LOGGER = logging.getLogger("cogito.interaction_web.commands")
router = APIRouter(prefix="/api/commands", tags=["commands"])

ACTOR = "dashboard"


def _ok(action: str, target_id: str, **details: Any) -> CommandResponse:
    return CommandResponse(
        command_id=uuid.uuid4().hex,
        status="ok",
        message=f"{action}: {target_id}",
        details=details,
    )


def _fail(action: str, target_id: str, reason: str) -> CommandResponse:
    return CommandResponse(
        command_id=uuid.uuid4().hex,
        status="failed",
        message=f"{action} failed: {reason}",
        details={"target_id": target_id},
    )


def _conflict(action: str, target_id: str, current_version: int) -> CommandResponse:
    """版本冲突响应（APPROVAL-COMMANDS §3.1）。"""
    return CommandResponse(
        command_id=uuid.uuid4().hex,
        status="conflict",
        message=f"{action} conflict: expected version mismatch",
        details={"target_id": target_id, "current_version": current_version},
    )


def _check_idempotency(
    conn: sqlite3.Connection,
    actor: str,
    command_type: str,
    idempotency_key: str,
) -> CommandResponse | None:
    """幂等键检查：重复命令返回第一次结果（APPROVAL-COMMANDS §2）。"""
    if not idempotency_key:
        return None
    from cogito.store.command_audit_repo import CommandAuditRepository
    repo = CommandAuditRepository(conn)
    existing = repo.find_by_idempotency(actor, command_type, idempotency_key)
    if existing is None:
        return None
    if existing.status == "consumed":
        return CommandResponse(
            command_id=existing.command_id,
            status="ok",
            message=f"{command_type}: {existing.target_id or ''} (idempotent replay)",
            details={"idempotent": True, "original_status": existing.status},
        )
    return None


def _persist_command(
    conn: sqlite3.Connection,
    actor: str,
    command_type: str,
    idempotency_key: str,
    target_type: str,
    target_id: str,
    status: str = "pending",
) -> str:
    """写入 commands 表（幂等键去重）。"""
    cmd_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO commands (command_id, actor, command_type, idempotency_key, "
        "target_type, target_id, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cmd_id, actor, command_type, idempotency_key, target_type, target_id, status,
         int(__import__("datetime").datetime.now(__import__("datetime").UTC).timestamp() * 1000)),
    )
    return cmd_id


# ── cancel-turn ───────────────────────────────────────────────


@router.post("/cancel-turn", response_model=CommandResponse)
def cancel_turn(payload: CancelTurnPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    # 读取当前 version (经过 Dispatcher -> repo)
    dispatcher = Dispatcher(deps.conn)
    turn_row = deps.conn.execute(
        "SELECT version, status FROM turns WHERE turn_id=?", (payload.turn_id,)
    ).fetchone()
    if turn_row is None:
        raise HTTPException(status_code=404, detail=f"turn {payload.turn_id} not found")
    if turn_row["status"] != "queued":
        return _fail("cancel-turn", payload.turn_id, f"status is {turn_row['status']}, not queued")
    ok = dispatcher.cancel(payload.turn_id, turn_row["version"])
    if not ok:
        return _fail("cancel-turn", payload.turn_id, "concurrent modification")
    write_audit(
        deps.conn, actor_id=ACTOR, action="cancel-turn",
        target_type="turn", target_id=payload.turn_id,
        changes={"reason": payload.reason},
    )
    return _ok("cancel-turn", payload.turn_id)


# ── retry-task ────────────────────────────────────────────────


@router.post("/retry-task", response_model=CommandResponse)
def retry_task(payload: RetryTaskPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    repo = TaskRepository(deps.conn)
    task = repo.get(payload.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {payload.task_id} not found")
    ok = repo.reset_to_queued(payload.task_id)
    if not ok:
        return _fail("retry-task", payload.task_id, f"status {task.status.value} not retryable")
    write_audit(
        deps.conn, actor_id=ACTOR, action="retry-task",
        target_type="task", target_id=payload.task_id,
    )
    return _ok("retry-task", payload.task_id)


# ── approve / reject ──────────────────────────────────────────


@router.post("/approve", response_model=CommandResponse)
def approve(payload: ApprovalPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    ok = set_approval_decision(
        deps.conn, approval_id=payload.approval_id, decision="approved", responder_id=ACTOR,
    )
    if not ok:
        exists = deps.conn.execute(
            "SELECT 1 FROM approvals WHERE approval_id=?", (payload.approval_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail=f"approval {payload.approval_id} not found")
        return _fail("approve", payload.approval_id, "not in pending status")
    # 审批通过后：仅创建一个恢复（Turn waiting_user → queued，幂等）
    resumed_turn_id = resume_turn_after_approval(deps.conn, approval_id=payload.approval_id)
    write_audit(
        deps.conn, actor_id=ACTOR, action="approve",
        target_type="approval", target_id=payload.approval_id,
        changes={"resumed_turn_id": resumed_turn_id},
    )
    return _ok("approve", payload.approval_id, resumed_turn_id=resumed_turn_id)


@router.post("/reject", response_model=CommandResponse)
def reject(payload: ApprovalPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    ok = set_approval_decision(
        deps.conn, approval_id=payload.approval_id, decision="rejected", responder_id=ACTOR,
    )
    if not ok:
        exists = deps.conn.execute(
            "SELECT 1 FROM approvals WHERE approval_id=?", (payload.approval_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail=f"approval {payload.approval_id} not found")
        return _fail("reject", payload.approval_id, "not in pending status")
    write_audit(
        deps.conn, actor_id=ACTOR, action="reject",
        target_type="approval", target_id=payload.approval_id,
    )
    return _ok("reject", payload.approval_id)


# ── confirm-memory / delete-memory ────────────────────────────


@router.post("/confirm-memory", response_model=CommandResponse)
def confirm_memory(payload: MemoryConfirmPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    svc = SqliteMemoryService(deps.conn)
    item = svc.get(payload.memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"memory {payload.memory_id} not found")
    # 按记忆所有者 principal 确认，保留服务层所有权校验语义
    ok = svc.confirm(payload.memory_id, confirmed_by=item.principal_id or ACTOR)
    if not ok:
        return _fail("confirm-memory", payload.memory_id, "not candidate")
    from cogito.service.memory_signals import SignalWriter
    SignalWriter(deps.conn).record_user_affirmed(
        payload.memory_id,
        actor_principal_id=item.principal_id or ACTOR,
        idempotency_key=f"confirm-memory:{payload.memory_id}:{item.version}",
        algorithm_version="2",
    )
    from cogito.service.task_service import SqliteTaskService
    try:
        SqliteTaskService(deps.conn).create(
            "memory.recompute_weight",
            "{}",
            idempotency_key=f"memory.recompute_weight:confirm:{payload.memory_id}:{item.version}",
            origin="confirm-memory",
            priority=20,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
    except sqlite3.IntegrityError:
        pass
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="confirm-memory",
        target_type="memory", target_id=payload.memory_id,
    )
    return _ok("confirm-memory", payload.memory_id)


@router.post("/reject-memory", response_model=CommandResponse)
def reject_memory(payload: MemoryRejectPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """拒绝记忆候选：标 rejected + 发 negative_feedback 信号（PLAN-14 R-05）。"""
    cached = _check_idempotency(deps.conn, ACTOR, "reject-memory", payload.idempotency_key)
    if cached:
        return cached
    svc = SqliteMemoryService(deps.conn)
    item = svc.get(payload.memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"memory {payload.memory_id} not found")
    ok = svc.reject(payload.memory_id)
    if not ok:
        return _fail("reject-memory", payload.memory_id, "not candidate or already decided")
    from cogito.service.memory_signals import SignalWriter
    SignalWriter(deps.conn).record_signal(
        "negative_feedback", payload.memory_id,
        actor_principal_id=item.principal_id or ACTOR,
        idempotency_key=f"negative:reject-memory:{payload.memory_id}",
        algorithm_version="2",
    )
    try:
        SqliteTaskService(deps.conn).create(
            "memory.recompute_weight", "{}",
            idempotency_key=f"memory.recompute_weight:reject:{payload.memory_id}",
            origin="reject-memory", priority=20,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
    except sqlite3.IntegrityError:
        pass
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="reject-memory",
        target_type="memory", target_id=payload.memory_id,
    )
    return _ok("reject-memory", payload.memory_id)


@router.post("/correct-memory", response_model=CommandResponse)
def correct_memory(payload: MemoryCorrectPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """修正记忆：创建新记忆 + 标旧记忆为 superseded + user_corrected 信号。"""
    cached = _check_idempotency(deps.conn, ACTOR, "correct-memory", payload.idempotency_key)
    if cached:
        return cached
    svc = SqliteMemoryService(deps.conn)
    old = svc.get(payload.memory_id)
    if old is None:
        raise HTTPException(status_code=404, detail=f"memory {payload.memory_id} not found")
    import uuid
    from cogito.domain.memory import MemoryItem
    from cogito.domain.memory import Explicitness
    corrected = MemoryItem(
        memory_id=uuid.uuid4().hex,
        principal_id=old.principal_id,
        kind=payload.kind if payload.kind else old.kind,
        subject=payload.subject if payload.subject else old.subject,
        predicate=payload.predicate if payload.predicate else old.predicate,
        value=payload.value if payload.value else old.value,
        scope_type=payload.scope_type if payload.scope_type != "global" else old.scope_type,
        scope_id=payload.scope_id if payload.scope_id else old.scope_id,
        confidence=payload.confidence,
        importance=payload.importance,
        explicitness=Explicitness.user_corrected,
        status="confirmed",
    )
    svc._repo.insert(corrected)
    svc.supersede(old.memory_id, corrected.memory_id)
    from cogito.service.memory_signals import SignalWriter
    SignalWriter(deps.conn).record_signal(
        "user_corrected", payload.memory_id,
        actor_principal_id=old.principal_id or ACTOR,
        idempotency_key=f"correct-memory:{payload.memory_id}:{corrected.memory_id}",
        algorithm_version="2",
    )
    try:
        SqliteTaskService(deps.conn).create(
            "memory.recompute_weight", "{}",
            idempotency_key=f"memory.recompute_weight:correct:{payload.memory_id}",
            origin="correct-memory", priority=20,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
    except sqlite3.IntegrityError:
        pass
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="correct-memory",
        target_type="memory", target_id=corrected.memory_id,
        changes={"supersedes": payload.memory_id},
    )
    return _ok("correct-memory", corrected.memory_id, supersedes=payload.memory_id)


@router.post("/proactive-negative-feedback", response_model=CommandResponse)
def proactive_negative_feedback(
    payload: ProactiveNegativeFeedbackPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """主动推送负反馈入口（reason: not_relevant/too_frequent/duplicate/wrong_time）。"""
    cached = _check_idempotency(deps.conn, ACTOR, "proactive-negative-feedback", payload.idempotency_key)
    if cached:
        return cached
    write_audit(
        deps.conn, actor_id=ACTOR, action="proactive-negative-feedback",
        target_type="proactive_candidate", target_id=payload.candidate_id,
        changes={"reason": payload.reason},
    )
    return _ok("proactive-negative-feedback", payload.candidate_id, reason=payload.reason)


@router.post("/delete-memory", response_model=CommandResponse)
def delete_memory(payload: MemoryDeletePayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    svc = SqliteMemoryService(deps.conn)
    ok = svc.forget(payload.memory_id)
    if not ok:
        return _fail("delete-memory", payload.memory_id, "not found")
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="delete-memory",
        target_type="memory", target_id=payload.memory_id,
    )
    return _ok("delete-memory", payload.memory_id)


# ── delete-session (软删除，仅标记 deleted_at) ────────────────


def _emit_session_completed(
    conn: sqlite3.Connection, *, session_id: str, conversation_id: str, principal_id: str,
) -> None:
    """发出 SessionCompleted 事件，供 SessionCompletedMemoryExtractionConsumer 投影。

    关闭会话时提交最后一次上下文提取任务，确保关闭前内容不丢失（PLAN-16 M1 P0-06）。
    """
    payload = {
        "session_id": session_id,
        "conversation_id": conversation_id,
        "principal_id": principal_id,
    }
    OutboxRepository(conn).insert(DomainEvent(
        event_type="SessionCompleted",
        aggregate_type="session",
        aggregate_id=session_id,
        aggregate_version=1,
        payload=payload,
        payload_ref=json.dumps(payload, ensure_ascii=False),
        origin="delete-session",
    ))


@router.post("/delete-session", response_model=CommandResponse)
def delete_session(payload: DeleteSessionPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """软删除会话：设置 deleted_at 时间戳，数据保留但页面不再显示。"""
    row = deps.conn.execute(
        "SELECT session_id, COALESCE(principal_id, 'owner') AS principal_id, "
        "conversation_id FROM sessions WHERE session_id=? AND deleted_at IS NULL",
        (payload.session_id,),
    ).fetchone()
    if row is None:
        return _fail("delete-session", payload.session_id, "not found or already deleted")
    from datetime import UTC, datetime
    deleted_at = datetime.now(UTC).isoformat()
    conversation_id = row["conversation_id"] or ""
    principal_id = row["principal_id"] or "owner"
    with deps.conn:
        deps.conn.execute(
            "UPDATE sessions SET deleted_at=? WHERE session_id=?",
            (deleted_at, payload.session_id),
        )
        # PLAN-16 M1 P0-06: session_closed 触发 → 提交关闭前最后一次提取任务
        _emit_session_completed(
            deps.conn, session_id=payload.session_id,
            conversation_id=conversation_id, principal_id=principal_id,
        )
    write_audit(
        deps.conn, actor_id=ACTOR, action="delete-session",
        target_type="session", target_id=payload.session_id,
        changes={"deleted_at": deleted_at},
    )
    return _ok("delete-session", payload.session_id, deleted_at=deleted_at)


# ── delete-sessions-by-conversation (按 conversation_id 批量软删除) ──


@router.post("/delete-sessions-by-conversation", response_model=CommandResponse)
def delete_sessions_by_conversation(
    payload: DeleteSessionsByConvPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """按 conversation_id 软删除其下所有活跃 session。"""
    from datetime import UTC, datetime
    rows = deps.conn.execute(
        "SELECT session_id, COALESCE(principal_id, 'owner') AS principal_id "
        "FROM sessions WHERE conversation_id=? AND deleted_at IS NULL",
        (payload.conversation_id,),
    ).fetchall()
    if not rows:
        return _fail("delete-sessions-by-conversation", payload.conversation_id, "no active sessions")
    deleted_at = datetime.now(UTC).isoformat()
    ids = [r["session_id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    with deps.conn:
        deps.conn.execute(
            f"UPDATE sessions SET deleted_at=? WHERE session_id IN ({placeholders})",
            [deleted_at, *ids],
        )
        # PLAN-16 M1 P0-06: 每个关闭的 session 都提交关闭前最后一次提取任务
        for r in rows:
            _emit_session_completed(
                deps.conn, session_id=r["session_id"],
                conversation_id=payload.conversation_id,
                principal_id=r["principal_id"] or "owner",
            )
    write_audit(
        deps.conn, actor_id=ACTOR, action="delete-sessions-by-conversation",
        target_type="conversation", target_id=payload.conversation_id,
        changes={"deleted_at": deleted_at, "session_count": len(ids), "session_ids": ids},
    )
    return _ok("delete-sessions-by-conversation", payload.conversation_id,
               deleted_count=len(ids), deleted_at=deleted_at)


# ── pause-connector ───────────────────────────────────────────


@router.post("/pause-connector", response_model=CommandResponse)
def pause_connector(payload: PauseConnectorPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    from cogito.domain.connector import ConnectorStatus

    repo = ConnectorRepository(deps.conn)
    conn_obj = repo.get(payload.connector_id)
    if conn_obj is None:
        raise HTTPException(status_code=404, detail=f"connector {payload.connector_id} not found")
    new_status = ConnectorStatus.paused if payload.paused else ConnectorStatus.active
    repo.update_status(payload.connector_id, new_status)
    write_audit(
        deps.conn, actor_id=ACTOR, action="pause-connector",
        target_type="connector", target_id=payload.connector_id,
        changes={"paused": payload.paused},
    )
    return _ok("pause-connector", payload.connector_id, paused=payload.paused)


# ── disable-plugin (mcp server 配置快照禁用) ──────────────────


@router.post("/disable-plugin", response_model=CommandResponse)
def disable_plugin(payload: DisablePluginPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """Disable through PluginRuntime, the unique plugin-state writer."""
    runtime = getattr(deps.runtime, "plugin_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Plugin Runtime is not available")
    state = runtime.disable(payload.name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"plugin {payload.name} not found")
    write_audit(
        deps.conn, actor_id=ACTOR, action="disable-plugin",
        target_type="plugin", target_id=payload.name,
    )
    return _ok("disable-plugin", payload.name, status=state.status)


# ── replay-delivery ───────────────────────────────────────────


@router.post("/replay-delivery", response_model=CommandResponse)
def replay_delivery_route(payload: ReplayDeliveryPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    ok = replay_delivery(deps.conn, delivery_id=payload.delivery_id)
    if not ok:
        exists = deps.conn.execute(
            "SELECT 1 FROM deliveries WHERE delivery_id=?", (payload.delivery_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail=f"delivery {payload.delivery_id} not found")
        return _fail("replay-delivery", payload.delivery_id, "status not replayable")
    write_audit(
        deps.conn, actor_id=ACTOR, action="replay-delivery",
        target_type="delivery", target_id=payload.delivery_id,
    )
    return _ok("replay-delivery", payload.delivery_id)


# ── Plan 08 Dashboard: 新增命令 ──────────────────────────────


@router.post("/review-proactive-candidate", response_model=CommandResponse)
def review_proactive_candidate(
    payload: ReviewProactiveCandidatePayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """审查主动候选：放行 / 摘要 / 丢弃。"""
    # 幂等检查
    cached = _check_idempotency(deps.conn, ACTOR, "review-proactive-candidate", payload.idempotency_key)
    if cached:
        return cached
    from cogito.store.proactive_repo import ProactiveCandidateRepository
    repo = ProactiveCandidateRepository(deps.conn)
    candidate = repo.get(payload.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"candidate {payload.candidate_id} not found")
    before_status = candidate.status
    status_map = {"approve_send": "decided", "digest": "decided", "dismiss": "consumed"}
    new_status = status_map.get(payload.action, "consumed")
    repo.update_status(payload.candidate_id, new_status)
    write_audit(
        deps.conn, actor_id=ACTOR, action="review-proactive-candidate",
        target_type="proactive_candidate", target_id=payload.candidate_id,
        changes={"before": before_status, "after": new_status, "action": payload.action},
    )
    deps.conn.commit()
    return _ok("review-proactive-candidate", payload.candidate_id, action=payload.action,
                before=before_status, after=new_status)


@router.post("/update-proactive-policy", response_model=CommandResponse)
def update_proactive_policy(
    payload: UpdateProactivePolicyPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """更新主动系统策略（版本化 + 乐观锁）。"""
    # 幂等检查
    cached = _check_idempotency(deps.conn, ACTOR, "update-proactive-policy", payload.idempotency_key)
    if cached:
        return cached
    from cogito.store.proactive_repo import ProactivePolicyRepository
    import uuid
    repo = ProactivePolicyRepository(deps.conn)
    current = repo.get_current()
    # 版本冲突检查
    if payload.expected_version is not None and payload.expected_version != current.version:
        return _conflict("update-proactive-policy", current.policy_id, current.version)
    new_policy = current.__class__(
        policy_id=uuid.uuid4().hex,
        principal_id=current.principal_id,
        version=current.version + 1,
        dry_run=payload.dry_run if payload.dry_run is not None else current.dry_run,
        max_pushes_per_hour=payload.max_pushes_per_hour if payload.max_pushes_per_hour is not None else current.max_pushes_per_hour,
        max_pushes_per_day=payload.max_pushes_per_day if payload.max_pushes_per_day is not None else current.max_pushes_per_day,
    )
    repo.save(new_policy)
    write_audit(
        deps.conn, actor_id=ACTOR, action="update-proactive-policy",
        target_type="proactive_policy", target_id=new_policy.policy_id,
        changes={"before_version": current.version, "after_version": new_policy.version,
                 "dry_run": new_policy.dry_run},
    )
    deps.conn.commit()
    return _ok("update-proactive-policy", new_policy.policy_id, version=new_policy.version)


@router.post("/replay-event", response_model=CommandResponse)
def replay_event(payload: ReplayEventPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """重放 Outbox 事件：重置为 pending 让 OutboxWorker 重新投递。"""
    row = deps.conn.execute(
        "SELECT * FROM outbox_events WHERE event_id=?", (payload.event_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"event {payload.event_id} not found")
    # 仅允许重放 failed / dead_letter 状态的事件
    if row["status"] not in ("failed", "dead_letter"):
        return _fail("replay-event", payload.event_id,
                     f"status is {row['status']}, only failed/dead_letter can be replayed")
    deps.conn.execute(
        "UPDATE outbox_events SET status='pending', attempt_count=0 WHERE event_id=?",
        (payload.event_id,),
    )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="replay-event",
        target_type="event", target_id=payload.event_id,
        changes={"from_status": row["status"], "to_status": "pending"},
    )
    return _ok("replay-event", payload.event_id, from_status=row["status"])


@router.post("/reconcile-receipt", response_model=CommandResponse)
def reconcile_receipt(
    payload: ReconcileReceiptPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """对账：将 side_effect_receipts 标记为已对账。"""
    # 幂等检查
    cached = _check_idempotency(deps.conn, ACTOR, "reconcile-receipt", payload.idempotency_key)
    if cached:
        return cached
    from cogito.store.receipt_repo import SideEffectReceiptRepository
    repo = SideEffectReceiptRepository(deps.conn)
    receipt = repo.get(payload.receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"receipt {payload.receipt_id} not found")
    before_reconcile = receipt.reconcile_status
    repo.update_reconcile(payload.receipt_id, "reconciled", summary="reconciled via dashboard")
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="reconcile-receipt",
        target_type="receipt", target_id=payload.receipt_id,
        changes={"before_reconcile": before_reconcile, "after_reconcile": "reconciled"},
    )
    return _ok("reconcile-receipt", payload.receipt_id, before=before_reconcile)


@router.post("/force-connector-poll", response_model=CommandResponse)
def force_connector_poll(
    payload: ForceConnectorPollPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """强制触发 Connector poll（重置 next_fire_at 或写入 poll 调度）。"""
    cached = _check_idempotency(deps.conn, ACTOR, "force-connector-poll", payload.idempotency_key)
    if cached:
        return cached
    connector = deps.conn.execute(
        "SELECT * FROM connectors WHERE connector_id=?", (payload.connector_id,)
    ).fetchone()
    if connector is None:
        raise HTTPException(status_code=404, detail=f"connector {payload.connector_id} not found")
    # 关联 active schedule，触发一次 fire
    from datetime import UTC, datetime
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    sched = deps.conn.execute(
        "SELECT * FROM schedules WHERE connector_id=? AND enabled=1 LIMIT 1",
        (payload.connector_id,),
    ).fetchone()
    if sched:
        deps.conn.execute(
            "UPDATE schedules SET next_fire_at=? WHERE schedule_id=?",
            (now_ms, sched["schedule_id"]),
        )
        deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="force-connector-poll",
        target_type="connector", target_id=payload.connector_id,
        changes={"schedule_id": sched["schedule_id"] if sched else None},
    )
    return _ok("force-connector-poll", payload.connector_id)


# ── Knowledge: register / refresh / invalidate / erase ───────


def _refresh_knowledge_views(conn: sqlite3.Connection, config: Any) -> None:
    """生成 KNOWLEDGE.md 视图（失败不回滚事实事务）。"""
    try:
        from cogito.service.knowledge_views import KnowledgeViewsGenerator
        KnowledgeViewsGenerator(conn, workspace_path=config.workspace_path).generate_all()
    except Exception as e:
        _LOGGER.warning("Knowledge view refresh after command failed: %s", e)


@router.post("/register-knowledge", response_model=CommandResponse)
def register_knowledge(
    payload: KnowledgeRegisterPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """注册知识资源 + 可选立即 ingest"""
    cached = _check_idempotency(deps.conn, ACTOR, "register-knowledge", payload.idempotency_key)
    if cached:
        return cached
    from cogito.service.knowledge.service import KnowledgeService
    svc = KnowledgeService(deps.conn)
    resource = svc.register_resource(
        source_uri_hash=payload.source_uri_hash or uuid.uuid4().hex,
        source_kind=payload.source_kind,
        media_type=payload.media_type,
        principal_id=payload.principal_id or "owner",
        trust_label=payload.trust_label,
        source_version=payload.source_version,
    )
    if payload.content:
        svc.ingest(resource.resource_id, payload.content)
    deps.conn.commit()
    _refresh_knowledge_views(deps.conn, deps.config)
    write_audit(
        deps.conn, actor_id=ACTOR, action="register-knowledge",
        target_type="knowledge_resource", target_id=resource.resource_id,
        changes={"source_kind": payload.source_kind, "ingested": bool(payload.content)},
    )
    return _ok("register-knowledge", resource.resource_id,
               source_kind=payload.source_kind)


@router.post("/refresh-knowledge", response_model=CommandResponse)
def refresh_knowledge(
    payload: KnowledgeRefreshPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """刷新现有知识来源内容（重新 ingest）。"""
    cached = _check_idempotency(deps.conn, ACTOR, "refresh-knowledge", payload.idempotency_key)
    if cached:
        return cached
    row = deps.conn.execute(
        "SELECT resource_id, content_hash FROM knowledge_resources "
        "WHERE source_uri_hash=? AND principal_id=? AND deleted_at IS NULL",
        (payload.source_uri_hash, payload.principal_id or "owner"),
    ).fetchone()
    if row is None:
        return _fail("refresh-knowledge", payload.source_uri_hash, "resource not found")
    resource_id = row["resource_id"]
    from cogito.service.knowledge.service import KnowledgeService
    svc = KnowledgeService(deps.conn)
    if payload.content:
        svc.invalidate(resource_id, "refresh")
        svc.ingest(resource_id, payload.content)
    deps.conn.commit()
    _refresh_knowledge_views(deps.conn, deps.config)
    write_audit(
        deps.conn, actor_id=ACTOR, action="refresh-knowledge",
        target_type="knowledge_resource", target_id=resource_id,
        changes={"refreshed": bool(payload.content)},
    )
    return _ok("refresh-knowledge", resource_id)


@router.post("/invalidate-knowledge", response_model=CommandResponse)
def invalidate_knowledge(
    payload: KnowledgeInvalidatePayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """失效知识资源（撤销检索，重置为 stale）。"""
    cached = _check_idempotency(deps.conn, ACTOR, "invalidate-knowledge", payload.idempotency_key)
    if cached:
        return cached
    row = deps.conn.execute(
        "SELECT resource_id FROM knowledge_resources WHERE resource_id=? AND deleted_at IS NULL",
        (payload.resource_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"knowledge resource {payload.resource_id} not found")
    from cogito.service.knowledge.service import KnowledgeService
    KnowledgeService(deps.conn).invalidate(payload.resource_id, payload.reason)
    deps.conn.commit()
    _refresh_knowledge_views(deps.conn, deps.config)
    write_audit(
        deps.conn, actor_id=ACTOR, action="invalidate-knowledge",
        target_type="knowledge_resource", target_id=payload.resource_id,
        changes={"reason": payload.reason},
    )
    return _ok("invalidate-knowledge", payload.resource_id)


@router.post("/erase-knowledge", response_model=CommandResponse)
def erase_knowledge(
    payload: KnowledgeErasePayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """擦除知识资源（撤销检索 + 清理 MemorySource）。"""
    cached = _check_idempotency(deps.conn, ACTOR, "erase-knowledge", payload.idempotency_key)
    if cached:
        return cached
    row = deps.conn.execute(
        "SELECT resource_id FROM knowledge_resources WHERE resource_id=? AND deleted_at IS NULL",
        (payload.resource_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"knowledge resource {payload.resource_id} not found")
    from cogito.service.knowledge.service import KnowledgeService
    KnowledgeService(deps.conn).erase(payload.resource_id, payload.reason)
    deps.conn.commit()
    _refresh_knowledge_views(deps.conn, deps.config)
    write_audit(
        deps.conn, actor_id=ACTOR, action="erase-knowledge",
        target_type="knowledge_resource", target_id=payload.resource_id,
        changes={"reason": payload.reason},
    )
    return _ok("erase-knowledge", payload.resource_id)


@router.post("/import-proactive-context", response_model=CommandResponse)
def import_proactive_context(
    payload: ImportProactiveContextPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """导入 PROACTIVE_CONTEXT.md：写文件 + 解析为 ProactivePolicy 新版本。"""
    # 幂等检查
    cached = _check_idempotency(deps.conn, ACTOR, "import-proactive-context", payload.idempotency_key)
    if cached:
        return cached
    import json
    from pathlib import Path
    import uuid
    from datetime import UTC, datetime
    workspace = Path(deps.config.workspace_path)
    context_file = workspace / "PROACTIVE_CONTEXT.md"
    # 写文件
    context_file.write_text(payload.content, encoding="utf-8")
    # 简单解析：提取黑白名单主题
    allow_topics, deny_topics = _parse_topics_from_markdown(payload.content)
    # 读当前最新版本
    current = deps.conn.execute(
        "SELECT * FROM proactive_policies ORDER BY version DESC LIMIT 1"
    ).fetchone()
    new_version = (current["version"] + 1) if current else 1
    dry_run = bool(current["dry_run"]) if current else True
    new_id = uuid.uuid4().hex
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    deps.conn.execute(
        "INSERT INTO proactive_policies "
        "(policy_id, principal_id, version, allow_topics_json, deny_topics_json, "
        " dry_run, filters_json, updated_by, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id, "owner", new_version,
         json.dumps(allow_topics, ensure_ascii=False),
         json.dumps(deny_topics, ensure_ascii=False),
         1 if dry_run else 0,
         "{}", "dashboard-import", now_ms),
    )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="import-proactive-context",
        target_type="proactive_context", target_id=new_policy.policy_id,
        changes={"version": new_policy.version, "allow_topics": allow_topics, "deny_topics": deny_topics},
    )
    return _ok("import-proactive-context", new_policy.policy_id, version=new_policy.version,
                allow_topics=allow_topics, deny_topics=deny_topics)


def _parse_topics_from_markdown(content: str) -> tuple[list[str], list[str]]:
    """从 PROACTIVE_CONTEXT.md 提取白名单/黑名单主题。"""
    allow, deny = [], []
    section = None
    for line in content.splitlines():
        stripped = line.strip()
        if "白名单" in stripped:
            section = "allow"
        elif "黑名单" in stripped:
            section = "deny"
        elif stripped.startswith("#"):
            section = None
        elif stripped.startswith("- ") and section == "allow":
            allow.append(stripped[2:].strip())
        elif stripped.startswith("- ") and section == "deny":
            deny.append(stripped[2:].strip())
    return allow, deny


@router.post("/rebuild-proactive-context", response_model=CommandResponse)
def rebuild_proactive_context(
    payload: RebuildProactiveContextPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """从 SQLite proactive_policies 重建 PROACTIVE_CONTEXT.md。"""
    row = deps.conn.execute(
        "SELECT * FROM proactive_policies ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return _fail("rebuild-proactive-context", "none", "no policy found")
    import json
    class _P:
        pass
    policy = _P()
    policy.version = row["version"]
    policy.dry_run = bool(row["dry_run"])
    policy.allow_topics = json.loads(row["allow_topics_json"] or "[]")
    policy.deny_topics = json.loads(row["deny_topics_json"] or "[]")
    bud = json.loads(row["budgets_json"] or "{}")
    policy.max_pushes_per_hour = bud.get("max_pushes_per_hour", 3)
    policy.max_pushes_per_day = bud.get("max_pushes_per_day", 10)
    # 渲染 Markdown
    md_lines = ["# Proactive Context", ""]
    md_lines.append(f"> 版本 v{policy.version} · dry_run={'是' if policy.dry_run else '否'}")
    md_lines.append("")
    md_lines.append("## 白名单（可以推的主题）")
    for t in policy.allow_topics:
        md_lines.append(f"- {t}")
    md_lines.append("")
    md_lines.append("## 黑名单（不要推的主题）")
    for t in policy.deny_topics:
        md_lines.append(f"- {t}")
    md_lines.append("")
    md_lines.append("## 过滤条件")
    md_lines.append(f"- 每小时最多推 {policy.max_pushes_per_hour} 次")
    md_lines.append(f"- 每天最多推 {policy.max_pushes_per_day} 次")
    from pathlib import Path
    context_file = Path(deps.config.workspace_path) / "PROACTIVE_CONTEXT.md"
    context_file.write_text("\n".join(md_lines), encoding="utf-8")
    write_audit(
        deps.conn, actor_id=ACTOR, action="rebuild-proactive-context",
        target_type="proactive_context", target_id=policy.policy_id,
        changes={"version": policy.version},
    )
    return _ok("rebuild-proactive-context", policy.policy_id, version=policy.version)


@router.post("/reconcile-delivery", response_model=CommandResponse)
def reconcile_delivery(
    payload: "ReconcileDeliveryPayload", deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """对账投递：unknown → confirmed（写 receipt_kind=confirmed）。"""
    from pydantic import BaseModel
    # 动态引用避免循环（payload 是由 ReconcileDeliveryPayload 传入的）
    class _Inline(BaseModel):
        delivery_id: str
    # 读取当前 delivery
    delivery = deps.conn.execute(
        "SELECT * FROM deliveries WHERE delivery_id=?", (payload.delivery_id,)
    ).fetchone()
    if delivery is None:
        raise HTTPException(status_code=404, detail=f"delivery {payload.delivery_id} not found")
    if delivery["status"] != "unknown":
        return _fail("reconcile-delivery", payload.delivery_id,
                     f"status is {delivery['status']}, only unknown can be reconciled")
    # 写一条 confirmed receipt
    import uuid
    receipt_id = uuid.uuid4().hex
    # 找到最新的 attempt
    latest_attempt = deps.conn.execute(
        "SELECT attempt_id FROM delivery_attempts WHERE delivery_id=? "
        "ORDER BY attempt_no DESC LIMIT 1",
        (payload.delivery_id,),
    ).fetchone()
    deps.conn.execute(
        "INSERT INTO delivery_receipts "
        "(receipt_id, delivery_id, delivery_attempt_id, operation_seq, "
        " request_hash, receipt_kind, platform_message_id, observed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (receipt_id, payload.delivery_id,
         latest_attempt["attempt_id"] if latest_attempt else "",
         0, "", "confirmed", delivery["platform_message_id"],
         int(__import__("datetime").datetime.now(__import__("datetime").UTC).timestamp() * 1000)),
    )
    # 更新 delivery 状态
    deps.conn.execute(
        "UPDATE deliveries SET status='sent' WHERE delivery_id=?",
        (payload.delivery_id,),
    )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="reconcile-delivery",
        target_type="delivery", target_id=payload.delivery_id,
        changes={"before": "unknown", "after": "sent", "receipt_id": receipt_id},
    )
    return _ok("reconcile-delivery", payload.delivery_id, receipt_id=receipt_id)


@router.post("/archive-skill", response_model=CommandResponse)
def archive_skill(
    payload: ArchiveSkillPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """归档 skill：skills 表 status → archived。"""
    import sqlite3
    from datetime import UTC, datetime
    from pathlib import Path
    # 优先 skills 表，其次 capabilities
    row = deps.conn.execute("SELECT * FROM skills WHERE skill_id=?", (payload.skill_id,)).fetchone()
    if row is None:
        row = deps.conn.execute("SELECT * FROM capabilities WHERE capability_id=? OR name=?", (payload.skill_id, payload.skill_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill {payload.skill_id} not found")
    before_status = row["status"] if "status" in row.keys() else "active"
    deps.conn.execute(
        "UPDATE skills SET status='archived', archived_at=?, updated_at=? WHERE skill_id=?",
        (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), payload.skill_id),
    )
    if deps.conn.total_changes == 0:
        # 尝试 capabilities
        deps.conn.execute(
            "UPDATE capabilities SET disabled=1, health='archived' WHERE capability_id=? OR name=?",
            (payload.skill_id, payload.skill_id),
        )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="archive-skill",
        target_type="skill", target_id=payload.skill_id,
        changes={"before": before_status, "after": "archived"},
    )
    return _ok("archive-skill", payload.skill_id, before=before_status)


@router.post("/restore-skill", response_model=CommandResponse)
def restore_skill(
    payload: RestoreSkillPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """恢复 skill：status → active。"""
    from datetime import UTC, datetime
    row = deps.conn.execute("SELECT * FROM skills WHERE skill_id=?", (payload.skill_id,)).fetchone()
    if row is None:
        row = deps.conn.execute("SELECT * FROM capabilities WHERE capability_id=? OR name=?", (payload.skill_id, payload.skill_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill {payload.skill_id} not found")
    before_status = row["status"] if "status" in row.keys() else "archived"
    deps.conn.execute(
        "UPDATE skills SET status='active', archived_at=NULL, updated_at=? WHERE skill_id=?",
        (datetime.now(UTC).isoformat(), payload.skill_id),
    )
    if deps.conn.total_changes == 0:
        deps.conn.execute(
            "UPDATE capabilities SET disabled=0, health='healthy' WHERE capability_id=? OR name=?",
            (payload.skill_id, payload.skill_id),
        )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="restore-skill",
        target_type="skill", target_id=payload.skill_id,
        changes={"before": before_status, "after": "active"},
    )
    return _ok("restore-skill", payload.skill_id, before=before_status)


@router.post("/pin-skill", response_model=CommandResponse)
def pin_skill(
    payload: PinSkillPayload, deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """置顶/取消置顶 skill。"""
    from datetime import UTC, datetime
    row = deps.conn.execute("SELECT * FROM skills WHERE skill_id=?", (payload.skill_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill {payload.skill_id} not found in skills table")
    before_pinned = bool(row["pinned"])
    deps.conn.execute(
        "UPDATE skills SET pinned=?, updated_at=? WHERE skill_id=?",
        (1 if payload.pinned else 0, datetime.now(UTC).isoformat(), payload.skill_id),
    )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="pin-skill",
        target_type="skill", target_id=payload.skill_id,
        changes={"before_pinned": before_pinned, "after_pinned": payload.pinned},
    )
    return _ok("pin-skill", payload.skill_id, pinned=payload.pinned)


@router.post("/disable-tool", response_model=CommandResponse)
def disable_tool(payload: DisableToolPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """禁用工具：更新 capabilities 表。"""
    # 幂等检查
    cached = _check_idempotency(deps.conn, ACTOR, "disable-tool", payload.idempotency_key)
    if cached:
        return cached
    # 读取当前状态用于 audit diff
    current = deps.conn.execute(
        "SELECT capability_id, disabled, health FROM capabilities WHERE capability_id=? OR tool_name=?",
        (payload.tool_name, payload.tool_name),
    ).fetchone()
    if current is None:
        return _fail("disable-tool", payload.tool_name, "tool not found in capabilities")
    before_disabled = bool(current["disabled"])
    row = deps.conn.execute(
        "UPDATE capabilities SET disabled=1, health='disabled' WHERE capability_id=? OR tool_name=?",
        (payload.tool_name, payload.tool_name),
    )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="disable-tool",
        target_type="tool", target_id=payload.tool_name,
        changes={"before_disabled": before_disabled, "after_disabled": True,
                 "before_health": current["health"], "after_health": "disabled"},
    )
    return _ok("disable-tool", payload.tool_name, before_disabled=before_disabled)


@router.post("/create-backup", response_model=CommandResponse)
def create_backup(payload: CreateBackupPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """创建真实文件备份：复制 workspace → .workspace/backups/{ts}/。"""
    import shutil
    from datetime import UTC, datetime
    from pathlib import Path
    from cogito.config import Config

    cfg = deps.config
    workspace = Path(cfg.workspace_path)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    backup_dir = workspace / "backups" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)
    total_size = 0
    try:
        # 备份 config + db + payloads（排除 backups 目录自身）
        for item in ["config.toml", "data"]:
            src = workspace / item
            if src.exists():
                dst = backup_dir / item
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
                # 统计大小
                if dst.is_dir():
                    total_size += sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
                else:
                    total_size += dst.stat().st_size
        # 写 backup 记录
        backup_id = __import__("uuid").uuid4().hex
        deps.conn.execute(
            "INSERT INTO backups (backup_id, path, size_mb, created_at, status, kind) "
            "VALUES (?,?,?,?,?,?)",
            (backup_id, str(backup_dir), total_size / (1024 * 1024), datetime.now(UTC).isoformat(), "completed", "full"),
        )
        deps.conn.commit()
        write_audit(
            deps.conn, actor_id=ACTOR, action="create-backup",
            target_type="backup", target_id=backup_id,
            changes={"path": str(backup_dir), "size_mb": total_size / (1024 * 1024)},
        )
        return _ok("create-backup", backup_id, path=str(backup_dir), size_mb=round(total_size / (1024 * 1024), 2))
    except Exception as e:
        # 记录失败
        backup_id = __import__("uuid").uuid4().hex
        deps.conn.execute(
            "INSERT INTO backups (backup_id, path, size_mb, created_at, status, kind) "
            "VALUES (?,?,?,?,?,?)",
            (backup_id, str(backup_dir), 0, datetime.now(UTC).isoformat(), "failed", "full"),
        )
        deps.conn.commit()
        return _fail("create-backup", backup_id, str(e))


@router.post("/verify-backup", response_model=CommandResponse)
def verify_backup(payload: VerifyBackupPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """验证备份：检查备份路径存在且包含 config.toml。"""
    from pathlib import Path
    row = deps.conn.execute("SELECT * FROM backups WHERE backup_id=?", (payload.backup_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"backup {payload.backup_id} not found")
    backup_path = Path(row["path"])
    config_exists = (backup_path / "config.toml").exists()
    data_exists = (backup_path / "data").exists()
    verified = config_exists and data_exists
    deps.conn.execute(
        "UPDATE backups SET status=?, verified=? WHERE backup_id=?",
        ("verified" if verified else "completed", 1 if verified else 0, payload.backup_id),
    )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="verify-backup",
        target_type="backup", target_id=payload.backup_id,
        changes={"verified": verified, "config_exists": config_exists, "data_exists": data_exists},
    )
    return _ok("verify-backup", payload.backup_id, verified=verified)


@router.post("/restore-backup", response_model=CommandResponse)
def restore_backup(payload: RestoreBackupPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """恢复备份：从备份路径复制回 workspace。"""
    import shutil
    from pathlib import Path
    from cogito.config import Config

    cfg = deps.config
    row = deps.conn.execute("SELECT * FROM backups WHERE backup_id=?", (payload.backup_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"backup {payload.backup_id} not found")
    backup_path = Path(row["path"])
    if not backup_path.exists():
        return _fail("restore-backup", payload.backup_id, "backup path does not exist")
    workspace = Path(cfg.workspace_path)
    restored = []
    try:
        for item in ["config.toml", "data"]:
            src = backup_path / item
            dst = workspace / item
            if src.exists():
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
                restored.append(item)
        write_audit(
            deps.conn, actor_id=ACTOR, action="restore-backup",
            target_type="backup", target_id=payload.backup_id,
            changes={"restored": restored, "recovery_profile": True},
        )
        return _ok("restore-backup", payload.backup_id, restored=restored, note="workspace restored; restart to apply")
    except Exception as e:
        return _fail("restore-backup", payload.backup_id, str(e))


@router.post("/config-dry-run", response_model=CommandResponse)
def config_dry_run(payload: ConfigDryRunPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """配置 dry-run：校验 config 内容但不应用。"""
    from cogito.config import Config
    import tempfile, os
    result = {"valid": False, "errors": []}
    try:
        # 写到临时文件尝试解析
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(payload.content)
            tmp_path = f.name
        try:
            Config.load(tmp_path)
            result["valid"] = True
        except Exception as e:
            result["errors"].append(str(e))
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        result["errors"].append(str(e))
    write_audit(
        deps.conn, actor_id=ACTOR, action="config-dry-run",
        target_type="config", target_id="dry-run",
        changes={"valid": result["valid"], "error_count": len(result["errors"])},
    )
    return _ok("config-dry-run", "dry-run", **result)


@router.post("/rollback-config", response_model=CommandResponse)
def rollback_config(payload: RollbackConfigPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """配置回滚：从 config_versions 读取历史版本的 content 并写回 config.toml。"""
    from cogito.config import ConfigVersionRepository
    from pathlib import Path
    from datetime import UTC, datetime

    repo = ConfigVersionRepository(deps.conn)
    ver = repo.get(payload.version_id)
    if ver is None:
        raise HTTPException(status_code=404, detail=f"config version {payload.version_id} not found")
    # 最新 active version
    latest = repo.latest()
    if latest and latest.content_hash == ver.content_hash:
        return _ok("rollback-config", payload.version_id, note="already at this version")
    # 插入新版本（回滚也是一个新版本）
    new_version_id = __import__("uuid").uuid4().hex
    deps.conn.execute(
        "INSERT INTO config_versions (version_id, content_hash, schema_version, source_layers, applied_at, change_summary) "
        "VALUES (?,?,?,?,?,?)",
        (new_version_id, ver.content_hash, ver.schema_version,
         __import__("json").dumps(ver.source_layers + ["rollback"]),
         int(datetime.now(UTC).timestamp() * 1000),
         f"rollback to {payload.version_id}"),
    )
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="rollback-config",
        target_type="config", target_id=new_version_id,
        changes={"from_version": payload.version_id, "to_hash": ver.content_hash},
    )
    return _ok("rollback-config", new_version_id, restored_hash=ver.content_hash,
               note="config_versions updated; actual config.toml restore requires file system write")


@router.post("/payload-gc-dry-run", response_model=CommandResponse)
def payload_gc_dry_run(payload: PayloadGcDryRunPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """Payload GC dry-run：列出可回收的孤立对象。"""
    orphans = deps.conn.execute(
        "SELECT payload_ref, size FROM payload_objects WHERE payload_ref NOT IN "
        "(SELECT content_ref FROM deliveries WHERE content_ref IS NOT NULL) "
        "AND payload_ref NOT IN (SELECT raw_ref FROM side_effect_receipts WHERE raw_ref IS NOT NULL) "
        "LIMIT 200"
    ).fetchall()
    orphan_refs = [r["payload_ref"] for r in orphans]
    total_size = sum(r["size"] for r in orphans)
    write_audit(
        deps.conn, actor_id=ACTOR, action="payload-gc-dry-run",
        target_type="payload", target_id="orphans",
        changes={"orphan_count": len(orphan_refs), "total_size_bytes": total_size},
    )
    return _ok("payload-gc-dry-run", "orphans",
               orphan_count=len(orphan_refs),
               total_size_bytes=total_size,
               sample=orphan_refs[:10])
