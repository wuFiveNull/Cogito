"""Command API 路由 —— 可写命令。

ACCESS-DELIVERY §2.3。所有命令：
  - 接受幂等键 + 生成 command_id
  - 经过服务层 (Dispatcher / TaskRepository / SqliteMemoryService / ConnectorRepository ...)
  - 写入 audit_records
  - DB 增删改查一律走服务；handler 不直写 SQL (audit.py 的 write_audit 除外)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from cogito.contracts.models import (
    ApprovalPayload,
    ArchiveSkillPayload,
    CancelTurnPayload,
    CommandResponse,
    ConfigDryRunPayload,
    CreateBackupPayload,
    DeleteSessionPayload,
    DeleteSessionsByConvPayload,
    DisablePluginPayload,
    DisableToolPayload,
    FetchProactiveDataPayload,
    ForceConnectorPollPayload,
    ImportProactiveContextPayload,
    KnowledgeErasePayload,
    KnowledgeInvalidatePayload,
    KnowledgeRefreshPayload,
    KnowledgeRegisterPayload,
    MemoryConfirmPayload,
    MemoryCorrectPayload,
    MemoryDeletePayload,
    MemoryErasePayload,
    MemoryRejectPayload,
    PauseConnectorPayload,
    PayloadGcDryRunPayload,
    PinSkillPayload,
    ProactiveNegativeFeedbackPayload,
    RebuildProactiveContextPayload,
    ReconcileDeliveryPayload,
    ReconcileReceiptPayload,
    RestoreBackupPayload,
    RestoreSkillPayload,
    RetryTaskPayload,
    ReviewProactiveCandidatePayload,
    RollbackConfigPayload,
    TriggerProactiveMockPayload,
    UpdateProactivePolicyPayload,
    VerifyBackupPayload,
)
from cogito.domain.errors import ConcurrencyConflictError
from cogito.domain.event import Event, EventClass, EventContext
from cogito.service.api.audit import write_audit
from cogito.service.api.command_service import resume_turn_after_approval, set_approval_decision
from cogito.service.api.deps import CommandDeps, get_command_deps
from cogito.service.approval_service import SqliteApprovalService
from cogito.service.dispatcher import Dispatcher
from cogito.service.memory_service import SqliteMemoryService
from cogito.service.task_service import SqliteTaskService
from cogito.store.connector_repo import ConnectorRepository
from cogito.store.event_store import EventStore
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


def _write_erasure_receipt(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    reason: str,
) -> str:
    """为 memory erase 写入一条 Erasure Receipt（PLAN-16 M3 MEM-05）。

    返回 receipt_id，供 tombstone 引用与审计对账。
    """
    import hashlib
    import uuid as _uuid

    from cogito.contracts.clock import epoch_ms
    from cogito.store.receipt_repo import ReceiptRecord, SideEffectReceiptRepository

    receipt_id = f"rcpt-erase-{memory_id[:8]}-{_uuid.uuid4().hex[:8]}"
    request_hash = hashlib.sha256(f"erase:{memory_id}:{reason}".encode()).hexdigest()[:16]
    SideEffectReceiptRepository(conn).insert(
        ReceiptRecord(
            receipt_id=receipt_id,
            capability_id="memory",
            operation_id=memory_id,
            request_hash=request_hash,
            side_effect_class="non_retriable",
            status="succeeded",
            reconcile_status="not_needed",
            summary=f"memory erased: {reason}",
            attempt_type="run",
            created_at=epoch_ms(),
        )
    )
    return receipt_id


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
        (
            cmd_id,
            actor,
            command_type,
            idempotency_key,
            target_type,
            target_id,
            status,
            int(__import__("datetime").datetime.now(__import__("datetime").UTC).timestamp() * 1000),
        ),
    )
    return cmd_id


# ── cancel-turn ───────────────────────────────────────────────


@router.post("/cancel-turn", response_model=CommandResponse)
def cancel_turn(
    payload: CancelTurnPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    # 读取当前 version (经过 Dispatcher -> repo)
    dispatcher = Dispatcher(deps.conn)
    from cogito.store.event_replay import replay_turn
    from cogito.store.event_store import EventStore

    turn_stream = EventStore(deps.conn).read_stream("turn", payload.turn_id)
    turn_state = replay_turn(turn_stream, payload.turn_id)
    if turn_state is None:
        raise HTTPException(status_code=404, detail=f"turn {payload.turn_id} not found")
    if turn_state.status not in {"queued", "waiting_user", "waiting_external"}:
        return _fail(
            "cancel-turn", payload.turn_id, f"status is {turn_state.status}, not cancellable"
        )
    ok = dispatcher.cancel(payload.turn_id, turn_state.stream_version)
    if not ok:
        return _fail("cancel-turn", payload.turn_id, "concurrent modification")
    from cogito.service.delegation_lifecycle import DelegationLifecycleService

    cancelled_children = DelegationLifecycleService(deps.conn).cancel_for_parent(payload.turn_id)
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="cancel-turn",
        target_type="turn",
        target_id=payload.turn_id,
        changes={"reason": payload.reason, "cancelled_delegations": cancelled_children},
    )
    return _ok("cancel-turn", payload.turn_id)


# ── retry-task ────────────────────────────────────────────────


@router.post("/retry-task", response_model=CommandResponse)
def retry_task(
    payload: RetryTaskPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    repo = TaskRepository(deps.conn)
    task = repo.get(payload.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {payload.task_id} not found")
    ok = repo.reset_to_queued(payload.task_id)
    if not ok:
        return _fail("retry-task", payload.task_id, f"status {task.status.value} not retryable")
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="retry-task",
        target_type="task",
        target_id=payload.task_id,
    )
    return _ok("retry-task", payload.task_id)


# ── approve / reject ──────────────────────────────────────────


@router.post("/approve", response_model=CommandResponse)
def approve(
    payload: ApprovalPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    ok = set_approval_decision(
        deps.conn,
        approval_id=payload.approval_id,
        decision="approved",
        responder_id=ACTOR,
        expected_version=payload.expected_version,
        action_hash=payload.action_hash,
        response_reason=payload.response_reason,
    )
    if not ok:
        if SqliteApprovalService(deps.conn).get(payload.approval_id) is None:
            raise HTTPException(status_code=404, detail=f"approval {payload.approval_id} not found")
        return _fail("approve", payload.approval_id, "not in pending status")
    # 审批通过后：仅创建一个恢复（Turn waiting_user → queued，幂等）
    resumed_turn_id = resume_turn_after_approval(deps.conn, approval_id=payload.approval_id)
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="approve",
        target_type="approval",
        target_id=payload.approval_id,
        changes={"resumed_turn_id": resumed_turn_id},
        commit=False,
    )
    deps.conn.commit()
    return _ok("approve", payload.approval_id, resumed_turn_id=resumed_turn_id)


@router.post("/reject", response_model=CommandResponse)
def reject(
    payload: ApprovalPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    ok = set_approval_decision(
        deps.conn,
        approval_id=payload.approval_id,
        decision="rejected",
        responder_id=ACTOR,
        expected_version=payload.expected_version,
        response_reason=payload.response_reason,
    )
    if not ok:
        if SqliteApprovalService(deps.conn).get(payload.approval_id) is None:
            raise HTTPException(status_code=404, detail=f"approval {payload.approval_id} not found")
        return _fail("reject", payload.approval_id, "not in pending status")
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="reject",
        target_type="approval",
        target_id=payload.approval_id,
        changes={"response_reason": payload.response_reason},
        commit=False,
    )
    deps.conn.commit()
    return _ok("reject", payload.approval_id)


# ── confirm-memory / delete-memory ────────────────────────────


@router.post("/confirm-memory", response_model=CommandResponse)
def confirm_memory(
    payload: MemoryConfirmPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    svc = SqliteMemoryService(deps.conn)
    item = svc.get(payload.memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"memory {payload.memory_id} not found")
    # PLAN-16 MEM-06: expected_version 乐观锁预检（并发修改不覆盖新版本）
    if payload.expected_version is not None and item.version != payload.expected_version:
        return _conflict("confirm-memory", payload.memory_id, item.version)
    # 按记忆所有者 principal 确认，保留服务层所有权校验语义
    ok = svc.confirm(
        payload.memory_id,
        confirmed_by=item.principal_id or ACTOR,
        expected_version=payload.expected_version,
    )
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
        SqliteTaskService(deps.conn, event_sourced=True).create(
            "memory.recompute_weight",
            "{}",
            idempotency_key=f"memory.recompute_weight:confirm:{payload.memory_id}:{item.version}",
            origin="confirm-memory",
            priority=20,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
    except sqlite3.IntegrityError:
        pass
    # PLAN-16 M2 TX-06: 审计与事实（confirm + signal + weight task）在同一事务原子提交。
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="confirm-memory",
        target_type="memory",
        target_id=payload.memory_id,
        commit=False,
    )
    deps.conn.commit()
    return _ok("confirm-memory", payload.memory_id)


@router.post("/reject-memory", response_model=CommandResponse)
def reject_memory(
    payload: MemoryRejectPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """拒绝记忆候选：标 rejected + 发 negative_feedback 信号（PLAN-14 R-05）。"""
    cached = _check_idempotency(deps.conn, ACTOR, "reject-memory", payload.idempotency_key)
    if cached:
        return cached
    svc = SqliteMemoryService(deps.conn)
    item = svc.get(payload.memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"memory {payload.memory_id} not found")
    # PLAN-16 MEM-06: expected_version 乐观锁预检（并发修改不覆盖新版本）
    if payload.expected_version is not None and item.version != payload.expected_version:
        return _conflict("reject-memory", payload.memory_id, item.version)
    ok = svc.reject(payload.memory_id, expected_version=payload.expected_version)
    if not ok:
        return _fail("reject-memory", payload.memory_id, "not candidate or already decided")
    from cogito.service.memory_signals import SignalWriter

    SignalWriter(deps.conn).record_signal(
        "negative_feedback",
        payload.memory_id,
        actor_principal_id=item.principal_id or ACTOR,
        idempotency_key=f"negative:reject-memory:{payload.memory_id}",
        algorithm_version="2",
    )
    try:
        SqliteTaskService(deps.conn, event_sourced=True).create(
            "memory.recompute_weight",
            "{}",
            idempotency_key=f"memory.recompute_weight:reject:{payload.memory_id}",
            origin="reject-memory",
            priority=20,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
    except sqlite3.IntegrityError:
        pass
    # PLAN-16 M2 TX-06: 审计与事实在同一事务原子提交。
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="reject-memory",
        target_type="memory",
        target_id=payload.memory_id,
        commit=False,
    )
    deps.conn.commit()
    return _ok("reject-memory", payload.memory_id)


@router.post("/correct-memory", response_model=CommandResponse)
def correct_memory(
    payload: MemoryCorrectPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """修正记忆（PLAN-16 M3 MEM-03/04/06）：统一走 svc.correct() 写入口 + 乐观锁。"""
    cached = _check_idempotency(deps.conn, ACTOR, "correct-memory", payload.idempotency_key)
    if cached and cached.status == "ok":
        return cached
    svc = SqliteMemoryService(deps.conn)
    existing = svc.get(payload.memory_id)
    if existing is None:
        return _fail("correct-memory", payload.memory_id, "not found")
    # MEM-06: expected_version 校验
    if payload.expected_version is not None and existing.version != payload.expected_version:
        return _conflict("correct-memory", payload.memory_id, existing.version)
    try:
        corrected = svc.correct(
            memory_id=payload.memory_id,
            expected_version=payload.expected_version,
            kind=payload.kind or None,
            subject=payload.subject or None,
            predicate=payload.predicate or None,
            value=payload.value or None,
            scope_type=payload.scope_type if payload.scope_type != "global" else None,
            scope_id=payload.scope_id or None,
            confidence=payload.confidence,
            importance=payload.importance,
            corrected_by=existing.principal_id or ACTOR,
        )
    except ConcurrencyConflictError:
        return _conflict("correct-memory", payload.memory_id, existing.version)
    try:
        SqliteTaskService(deps.conn, event_sourced=True).create(
            "memory.recompute_weight",
            "{}",
            idempotency_key=f"memory.recompute_weight:correct:{payload.memory_id}",
            origin="correct-memory",
            priority=20,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
    except sqlite3.IntegrityError:
        pass
    # PLAN-16 M2/M3: 审计与事实（新记忆 + supersede + signal + weight task）同一事务原子提交。
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="correct-memory",
        target_type="memory",
        target_id=corrected.memory_id,
        changes={"supersedes": payload.memory_id},
        commit=False,
    )
    deps.conn.commit()
    return _ok("correct-memory", corrected.memory_id, supersedes=payload.memory_id)


@router.post("/proactive-negative-feedback", response_model=CommandResponse)
def proactive_negative_feedback(
    payload: ProactiveNegativeFeedbackPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """主动推送负反馈入口（reason: not_relevant/too_frequent/duplicate/wrong_time）。"""
    cached = _check_idempotency(
        deps.conn, ACTOR, "proactive-negative-feedback", payload.idempotency_key
    )
    if cached:
        return cached
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="proactive-negative-feedback",
        target_type="proactive_candidate",
        target_id=payload.candidate_id,
        changes={"reason": payload.reason},
    )
    return _ok("proactive-negative-feedback", payload.candidate_id, reason=payload.reason)


@router.post("/delete-memory", response_model=CommandResponse)
def delete_memory(
    payload: MemoryDeletePayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """软删除记忆（deprecated: 保留向后兼容，新语义请使用 erase-memory）。"""
    svc = SqliteMemoryService(deps.conn)
    ok = svc.forget(payload.memory_id)
    if not ok:
        return _fail("delete-memory", payload.memory_id, "not found")
    # PLAN-16 M2 TX-06: 审计与 forget 事实同一事务原子提交。
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="delete-memory",
        target_type="memory",
        target_id=payload.memory_id,
        commit=False,
    )
    deps.conn.commit()
    return _ok("delete-memory", payload.memory_id)


@router.post("/erase-memory", response_model=CommandResponse)
def erase_memory(
    payload: MemoryErasePayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """擦除记忆并生成 tombstone、Receipt、Audit 与领域事件。"""
    cached = _check_idempotency(deps.conn, ACTOR, "erase-memory", payload.idempotency_key)
    if cached and cached.status == "ok":
        return cached
    svc = SqliteMemoryService(deps.conn)
    existing = svc.get(payload.memory_id)
    if existing is None:
        return _fail("erase-memory", payload.memory_id, "not found")
    # expected_version 校验（MEM-06）：乐观锁防并发覆盖
    if payload.expected_version is not None and existing.version != payload.expected_version:
        return _conflict("erase-memory", payload.memory_id, existing.version)
    # 幂等：已擦除（deleted_at 非空）直接返回成功，不重复写 Receipt
    if existing.deleted_at is not None:
        return _ok("erase-memory", payload.memory_id, reason="already_erased")
    receipt_id = _write_erasure_receipt(
        deps.conn,
        memory_id=payload.memory_id,
        reason=payload.reason,
    )
    try:
        ok = svc.erase(
            memory_id=payload.memory_id,
            receipt_id=receipt_id,
            reason=payload.reason,
            expected_version=payload.expected_version,
            principal_id=None,
        )
    except ConcurrencyConflictError:
        return _conflict("erase-memory", payload.memory_id, existing.version)
    if not ok:
        return _fail("erase-memory", payload.memory_id, "not found")
    # PLAN-16 M2/3: 审计与事实 + 事件 + Receipt 同一事务原子提交
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="erase-memory",
        target_type="memory",
        target_id=payload.memory_id,
        changes={"reason": payload.reason, "receipt_id": receipt_id},
        commit=False,
    )
    deps.conn.commit()
    return _ok("erase-memory", payload.memory_id, reason=payload.reason, receipt_id=receipt_id)


# ── delete-session (软删除，仅标记 deleted_at) ────────────────


def _emit_session_completed(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    conversation_id: str,
    principal_id: str,
) -> None:
    """发出 SessionCompleted 事件，供 SessionCompletedMemoryExtractionConsumer 投影。

    关闭会话时提交最后一次上下文提取任务，确保关闭前内容不丢失（PLAN-16 M1 P0-06）。
    """
    EventStore(conn).append(
        Event(
            event_type="runtime.session.completed",
            stream_type="session",
            stream_id=session_id,
            producer="delete-session-command",
            event_class=EventClass.DOMAIN,
            context=EventContext(
                principal_id=principal_id,
                conversation_id=conversation_id,
                session_id=session_id,
            ),
            summary="Session completed",
            outcome="completed",
            idempotency_key=f"session:{session_id}:completed",
        )
    )


@router.post("/delete-session", response_model=CommandResponse)
def delete_session(
    payload: DeleteSessionPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """关闭 Session：存在 Event 流时只追加终结事实。"""
    from cogito.store.repositories import ConversationRepository, SessionRepository

    session = SessionRepository(deps.conn).find(payload.session_id)
    if session is not None:
        if session.status.value != "active":
            return _fail("delete-session", payload.session_id, "not found or already deleted")
        conversation = ConversationRepository(deps.conn).find(
            session.conversation_id
        )
        principal_id = conversation.principal_scope if conversation is not None else "owner"
        from datetime import UTC, datetime

        deleted_at = datetime.now(UTC).isoformat()
        with deps.conn:
            _emit_session_completed(
                deps.conn,
                session_id=session.session_id,
                conversation_id=session.conversation_id,
                principal_id=principal_id or "owner",
            )
        write_audit(
            deps.conn,
            actor_id=ACTOR,
            action="delete-session",
            target_type="session",
            target_id=payload.session_id,
            changes={"deleted_at": deleted_at},
        )
        return _ok("delete-session", payload.session_id, deleted_at=deleted_at)

    # Compatibility path for historical, pre-backfill rows.
    row = deps.conn.execute(
        "SELECT s.session_id, s.conversation_id, "
        "COALESCE(c.principal_scope, 'owner') AS principal_id "
        "FROM sessions s LEFT JOIN conversations c ON c.conversation_id=s.conversation_id "
        "WHERE s.session_id=? AND s.deleted_at IS NULL",
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
            deps.conn,
            session_id=payload.session_id,
            conversation_id=conversation_id,
            principal_id=principal_id,
        )
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="delete-session",
        target_type="session",
        target_id=payload.session_id,
        changes={"deleted_at": deleted_at},
    )
    return _ok("delete-session", payload.session_id, deleted_at=deleted_at)


# ── delete-sessions-by-conversation (按 conversation_id 批量软删除) ──


@router.post("/delete-sessions-by-conversation", response_model=CommandResponse)
def delete_sessions_by_conversation(
    payload: DeleteSessionsByConvPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """关闭某 Conversation 下的 Session；新数据不更新投影行。"""
    from cogito.store.repositories import ConversationRepository, SessionRepository
    from datetime import UTC, datetime

    event_sessions = SessionRepository(deps.conn).list_by_conversation(
        payload.conversation_id,
        active_only=True,
    )
    if event_sessions:
        conversation = ConversationRepository(deps.conn).find(
            payload.conversation_id
        )
        principal_id = conversation.principal_scope if conversation is not None else "owner"
        deleted_at = datetime.now(UTC).isoformat()
        with deps.conn:
            for session in event_sessions:
                _emit_session_completed(
                    deps.conn,
                    session_id=session.session_id,
                    conversation_id=session.conversation_id,
                    principal_id=principal_id or "owner",
                )
        ids = [session.session_id for session in event_sessions]
        write_audit(
            deps.conn,
            actor_id=ACTOR,
            action="delete-sessions-by-conversation",
            target_type="conversation",
            target_id=payload.conversation_id,
            changes={"deleted_at": deleted_at, "session_count": len(ids), "session_ids": ids},
        )
        return _ok(
            "delete-sessions-by-conversation",
            payload.conversation_id,
            deleted_count=len(ids),
            deleted_at=deleted_at,
        )

    # Compatibility path for historical, pre-backfill rows.
    rows = deps.conn.execute(
        "SELECT s.session_id, COALESCE(c.principal_scope, 'owner') AS principal_id "
        "FROM sessions s LEFT JOIN conversations c ON c.conversation_id=s.conversation_id "
        "WHERE s.conversation_id=? AND s.deleted_at IS NULL",
        (payload.conversation_id,),
    ).fetchall()
    if not rows:
        return _fail(
            "delete-sessions-by-conversation", payload.conversation_id, "no active sessions"
        )
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
                deps.conn,
                session_id=r["session_id"],
                conversation_id=payload.conversation_id,
                principal_id=r["principal_id"] or "owner",
            )
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="delete-sessions-by-conversation",
        target_type="conversation",
        target_id=payload.conversation_id,
        changes={"deleted_at": deleted_at, "session_count": len(ids), "session_ids": ids},
    )
    return _ok(
        "delete-sessions-by-conversation",
        payload.conversation_id,
        deleted_count=len(ids),
        deleted_at=deleted_at,
    )


# ── pause-connector ───────────────────────────────────────────


@router.post("/pause-connector", response_model=CommandResponse)
def pause_connector(
    payload: PauseConnectorPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    from cogito.domain.connector import ConnectorStatus

    repo = ConnectorRepository(deps.conn)
    conn_obj = repo.get(payload.connector_id)
    if conn_obj is None:
        raise HTTPException(status_code=404, detail=f"connector {payload.connector_id} not found")
    new_status = ConnectorStatus.paused if payload.paused else ConnectorStatus.active
    repo.update_status(payload.connector_id, new_status)
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="pause-connector",
        target_type="connector",
        target_id=payload.connector_id,
        changes={"paused": payload.paused},
    )
    return _ok("pause-connector", payload.connector_id, paused=payload.paused)


# ── disable-plugin (mcp server 配置快照禁用) ──────────────────


@router.post("/disable-plugin", response_model=CommandResponse)
def disable_plugin(
    payload: DisablePluginPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """Disable through PluginRuntime, the unique plugin-state writer."""
    runtime = getattr(deps.runtime, "plugin_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Plugin Runtime is not available")
    state = runtime.disable(payload.name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"plugin {payload.name} not found")
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="disable-plugin",
        target_type="plugin",
        target_id=payload.name,
    )
    return _ok("disable-plugin", payload.name, status=state.status)


# ── Plan 08 Dashboard: 新增命令 ──────────────────────────────


@router.post("/review-proactive-candidate", response_model=CommandResponse)
def review_proactive_candidate(
    payload: ReviewProactiveCandidatePayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """审查主动候选：放行 / 摘要 / 丢弃。"""
    # 幂等检查
    cached = _check_idempotency(
        deps.conn, ACTOR, "review-proactive-candidate", payload.idempotency_key
    )
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
        deps.conn,
        actor_id=ACTOR,
        action="review-proactive-candidate",
        target_type="proactive_candidate",
        target_id=payload.candidate_id,
        changes={"before": before_status, "after": new_status, "action": payload.action},
    )
    deps.conn.commit()
    return _ok(
        "review-proactive-candidate",
        payload.candidate_id,
        action=payload.action,
        before=before_status,
        after=new_status,
    )


@router.post("/update-proactive-policy", response_model=CommandResponse)
def update_proactive_policy(
    payload: UpdateProactivePolicyPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """更新主动系统策略（版本化 + 乐观锁）。"""
    # 幂等检查
    cached = _check_idempotency(
        deps.conn, ACTOR, "update-proactive-policy", payload.idempotency_key
    )
    if cached:
        return cached
    import uuid

    from cogito.store.proactive_repo import ProactivePolicyRepository

    repo = ProactivePolicyRepository(deps.conn)
    current = repo.get_current()
    # 版本冲突检查
    if payload.expected_version is not None and payload.expected_version != current.version:
        return _conflict("update-proactive-policy", current.policy_id, current.version)
    new_policy = replace(
        current,
        policy_id=uuid.uuid4().hex,
        version=current.version + 1,
        dry_run=payload.dry_run if payload.dry_run is not None else current.dry_run,
        max_pushes_per_hour=payload.max_pushes_per_hour
        if payload.max_pushes_per_hour is not None
        else current.max_pushes_per_hour,
        max_pushes_per_day=payload.max_pushes_per_day
        if payload.max_pushes_per_day is not None
        else current.max_pushes_per_day,
    )
    repo.save(new_policy)
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="update-proactive-policy",
        target_type="proactive_policy",
        target_id=new_policy.policy_id,
        changes={
            "before_version": current.version,
            "after_version": new_policy.version,
            "dry_run": new_policy.dry_run,
        },
    )
    deps.conn.commit()
    return _ok("update-proactive-policy", new_policy.policy_id, version=new_policy.version)


@router.post("/reconcile-receipt", response_model=CommandResponse)
def reconcile_receipt(
    payload: ReconcileReceiptPayload,
    deps: CommandDeps = Depends(get_command_deps),
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
        deps.conn,
        actor_id=ACTOR,
        action="reconcile-receipt",
        target_type="receipt",
        target_id=payload.receipt_id,
        changes={"before_reconcile": before_reconcile, "after_reconcile": "reconciled"},
    )
    return _ok("reconcile-receipt", payload.receipt_id, before=before_reconcile)


@router.post("/force-connector-poll", response_model=CommandResponse)
def force_connector_poll(
    payload: ForceConnectorPollPayload,
    deps: CommandDeps = Depends(get_command_deps),
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
        deps.conn,
        actor_id=ACTOR,
        action="force-connector-poll",
        target_type="connector",
        target_id=payload.connector_id,
        changes={"schedule_id": sched["schedule_id"] if sched else None},
    )
    return _ok("force-connector-poll", payload.connector_id)


@router.post("/fetch-proactive-data", response_model=CommandResponse)
def fetch_proactive_data(
    payload: FetchProactiveDataPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """排队一次 AIHOT Poll；后续 Candidate/Decision 仍走 Outbox + Task。"""
    from cogito.domain.task import Task, TaskStatus
    from cogito.service.aihot_connector import AIHOT_CONNECTOR_ID

    if not deps.config.capability.proactive.enabled:
        raise HTTPException(status_code=409, detail="proactive is disabled")
    connector = deps.conn.execute(
        "SELECT status FROM connectors WHERE connector_id=?",
        (AIHOT_CONNECTOR_ID,),
    ).fetchone()
    if connector is None or connector["status"] != "active":
        raise HTTPException(status_code=409, detail="AIHOT connector is not active")
    manager = getattr(deps.runtime, "mcp_manager", None)
    client = manager.get_client("aihot") if manager is not None else None
    if client is None or not client.connected:
        raise HTTPException(status_code=503, detail="AIHOT MCP server is not connected")

    request_key = payload.idempotency_key or uuid.uuid4().hex
    task_key = f"manual-proactive-fetch:{request_key}"
    existing = deps.conn.execute(
        "SELECT task_id FROM tasks WHERE idempotency_key=?",
        (task_key,),
    ).fetchone()
    if existing is not None:
        task_id = existing["task_id"]
        return _ok(
            "fetch-proactive-data",
            AIHOT_CONNECTOR_ID,
            poll_task_id=task_id,
            connector_id=AIHOT_CONNECTOR_ID,
            dry_run=deps.config.capability.proactive.dry_run,
            idempotent=True,
        )

    task = Task(
        task_id=f"task-aihot-manual-{uuid.uuid4().hex[:16]}",
        task_type="mcp_connector.poll",
        payload_ref=AIHOT_CONNECTOR_ID,
        status=TaskStatus.queued,
        priority=60,
        idempotency_key=task_key,
        origin="dashboard-proactive-fetch",
        retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
    )
    TaskRepository(deps.conn).insert(task)
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="fetch-proactive-data",
        target_type="connector",
        target_id=AIHOT_CONNECTOR_ID,
        changes={"poll_task_id": task.task_id, "dry_run": deps.config.capability.proactive.dry_run},
    )
    deps.conn.commit()
    return _ok(
        "fetch-proactive-data",
        AIHOT_CONNECTOR_ID,
        poll_task_id=task.task_id,
        connector_id=AIHOT_CONNECTOR_ID,
        dry_run=deps.config.capability.proactive.dry_run,
        idempotent=False,
    )


@router.post("/trigger-proactive-mock", response_model=CommandResponse)
def trigger_proactive_mock(
    payload: TriggerProactiveMockPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """Queue one manual-only mock source poll for end-to-end delivery checks."""
    from cogito.domain.task import Task, TaskStatus
    from cogito.service.proactive_mock_connector import (
        PROACTIVE_MOCK_CONNECTOR_ID,
        PROACTIVE_MOCK_SERVER_NAME,
    )

    if not deps.config.capability.proactive.enabled:
        raise HTTPException(status_code=409, detail="proactive is disabled")
    connector = deps.conn.execute(
        "SELECT status FROM connectors WHERE connector_id=?",
        (PROACTIVE_MOCK_CONNECTOR_ID,),
    ).fetchone()
    if connector is None or connector["status"] != "active":
        raise HTTPException(status_code=409, detail="proactive mock connector is not active")
    manager = getattr(deps.runtime, "mcp_manager", None)
    client = manager.get_client(PROACTIVE_MOCK_SERVER_NAME) if manager is not None else None
    if client is None or not client.connected:
        raise HTTPException(status_code=503, detail="proactive mock MCP server is not connected")

    request_key = payload.idempotency_key or uuid.uuid4().hex
    task_key = f"manual-proactive-mock:{request_key}"
    existing = deps.conn.execute(
        "SELECT task_id FROM tasks WHERE idempotency_key=?",
        (task_key,),
    ).fetchone()
    if existing is not None:
        return _ok(
            "trigger-proactive-mock",
            PROACTIVE_MOCK_CONNECTOR_ID,
            poll_task_id=existing["task_id"],
            connector_id=PROACTIVE_MOCK_CONNECTOR_ID,
            dry_run=deps.config.capability.proactive.dry_run,
            idempotent=True,
        )

    task = Task(
        task_id=f"task-proactive-mock-{uuid.uuid4().hex[:16]}",
        task_type="mcp_connector.poll",
        payload_ref=PROACTIVE_MOCK_CONNECTOR_ID,
        status=TaskStatus.queued,
        priority=10,
        idempotency_key=task_key,
        origin="dashboard-proactive-mock",
        retry_policy={"max_attempts": 1},
    )
    TaskRepository(deps.conn).insert(task)
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="trigger-proactive-mock",
        target_type="connector",
        target_id=PROACTIVE_MOCK_CONNECTOR_ID,
        changes={"poll_task_id": task.task_id, "dry_run": deps.config.capability.proactive.dry_run},
    )
    deps.conn.commit()
    return _ok(
        "trigger-proactive-mock",
        PROACTIVE_MOCK_CONNECTOR_ID,
        poll_task_id=task.task_id,
        connector_id=PROACTIVE_MOCK_CONNECTOR_ID,
        dry_run=deps.config.capability.proactive.dry_run,
        idempotent=False,
    )


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
    payload: KnowledgeRegisterPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """注册知识资源 + 可选经 durable Task 摄取（PLAN-16 M4 KNOW-03/04）。

    仅登记意图与注册资源（元数据，快速）；内容摄取经 durable
    knowledge.sync_source Task 完成（可恢复可重试，不在 HTTP 请求内同步 parse/embed）。
    """
    cached = _check_idempotency(deps.conn, ACTOR, "register-knowledge", payload.idempotency_key)
    if cached and cached.status == "ok":
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
    task_id = None
    if payload.content:
        # PLAN-16 M4 KNOW-03/04: 内容经 durable Task 摄取（handler 使用含 embedder 的工厂）
        from cogito.service.knowledge.sync import enqueue_knowledge_sync_source

        task_id = enqueue_knowledge_sync_source(
            deps.conn,
            stable_source_id=resource.source_uri_hash,
            source_kind=payload.source_kind,
            content_hash="",
            raw_text=payload.content,
            principal_id=payload.principal_id or "owner",
            trust_label=payload.trust_label,
            origin="register-knowledge",
        )
    # PLAN-16 M2 TX-05/06: 审计与事实同一事务原子提交。
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="register-knowledge",
        target_type="knowledge_resource",
        target_id=resource.resource_id,
        changes={"source_kind": payload.source_kind, "ingested": bool(payload.content)},
        commit=False,
    )
    deps.conn.commit()
    _refresh_knowledge_views(deps.conn, deps.config)
    return _ok(
        "register-knowledge", resource.resource_id, source_kind=payload.source_kind, task_id=task_id
    )


@router.post("/refresh-knowledge", response_model=CommandResponse)
def refresh_knowledge(
    payload: KnowledgeRefreshPayload,
    deps: CommandDeps = Depends(get_command_deps),
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
    task_id = None
    if payload.content:
        # PLAN-16 M4 KNOW-03/04: 失效 + 内容经 durable Task 重新摄取
        svc.invalidate(resource_id, "refresh")
        from cogito.service.knowledge.sync import enqueue_knowledge_sync_source

        task_id = enqueue_knowledge_sync_source(
            deps.conn,
            stable_source_id=payload.source_uri_hash,
            source_kind="explicit_local_file",
            content_hash="",
            raw_text=payload.content,
            principal_id=payload.principal_id or "owner",
            trust_label="unverified",
            origin="refresh-knowledge",
        )
    # PLAN-16 M2 TX-05/06: 审计与事实同一事务原子提交。
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="refresh-knowledge",
        target_type="knowledge_resource",
        target_id=resource_id,
        changes={"refreshed": bool(payload.content)},
        commit=False,
    )
    deps.conn.commit()
    _refresh_knowledge_views(deps.conn, deps.config)
    return _ok("refresh-knowledge", resource_id, task_id=task_id)


@router.post("/invalidate-knowledge", response_model=CommandResponse)
def invalidate_knowledge(
    payload: KnowledgeInvalidatePayload,
    deps: CommandDeps = Depends(get_command_deps),
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
        raise HTTPException(
            status_code=404, detail=f"knowledge resource {payload.resource_id} not found"
        )
    from cogito.service.knowledge.service import KnowledgeService

    KnowledgeService(deps.conn).invalidate(payload.resource_id, payload.reason)
    # PLAN-16 M2 TX-05/06: 审计与事实同一事务原子提交。
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="invalidate-knowledge",
        target_type="knowledge_resource",
        target_id=payload.resource_id,
        changes={"reason": payload.reason},
        commit=False,
    )
    deps.conn.commit()
    _refresh_knowledge_views(deps.conn, deps.config)
    return _ok("invalidate-knowledge", payload.resource_id)


@router.post("/erase-knowledge", response_model=CommandResponse)
def erase_knowledge(
    payload: KnowledgeErasePayload,
    deps: CommandDeps = Depends(get_command_deps),
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
        raise HTTPException(
            status_code=404, detail=f"knowledge resource {payload.resource_id} not found"
        )
    from cogito.service.knowledge.service import KnowledgeService

    KnowledgeService(deps.conn).erase(payload.resource_id, payload.reason)
    # PLAN-16 M2 TX-05/06: 审计与事实（含跨聚合 MemorySource 清理）同一事务原子提交。
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="erase-knowledge",
        target_type="knowledge_resource",
        target_id=payload.resource_id,
        changes={"reason": payload.reason},
        commit=False,
    )
    deps.conn.commit()
    _refresh_knowledge_views(deps.conn, deps.config)
    return _ok("erase-knowledge", payload.resource_id)


@router.post("/import-proactive-context", response_model=CommandResponse)
def import_proactive_context(
    payload: ImportProactiveContextPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """导入 PROACTIVE_CONTEXT.md：写文件 + 解析为 ProactivePolicy 新版本。"""
    # 幂等检查
    cached = _check_idempotency(
        deps.conn, ACTOR, "import-proactive-context", payload.idempotency_key
    )
    if cached:
        return cached
    from pathlib import Path

    from cogito.store.proactive_repo import ProactivePolicyRepository

    workspace = Path(deps.config.workspace_path)
    context_file = workspace / "PROACTIVE_CONTEXT.md"
    # 写文件
    context_file.write_text(payload.content, encoding="utf-8")
    # 简单解析：提取黑白名单主题
    allow_topics, deny_topics = _parse_topics_from_markdown(payload.content)
    # 仅替换上下文明确携带的主题字段，其余策略事实完整继承。
    repo = ProactivePolicyRepository(deps.conn)
    current = repo.get_current()
    new_policy = replace(
        current,
        policy_id=uuid.uuid4().hex,
        version=current.version + 1,
        allow_topics=tuple(allow_topics),
        deny_topics=tuple(deny_topics),
    )
    repo.save(new_policy)
    deps.conn.commit()
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="import-proactive-context",
        target_type="proactive_context",
        target_id=new_policy.policy_id,
        changes={
            "version": new_policy.version,
            "allow_topics": allow_topics,
            "deny_topics": deny_topics,
        },
    )
    return _ok(
        "import-proactive-context",
        new_policy.policy_id,
        version=new_policy.version,
        allow_topics=allow_topics,
        deny_topics=deny_topics,
    )


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
    payload: RebuildProactiveContextPayload,
    deps: CommandDeps = Depends(get_command_deps),
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
        deps.conn,
        actor_id=ACTOR,
        action="rebuild-proactive-context",
        target_type="proactive_context",
        target_id=policy.policy_id,
        changes={"version": policy.version},
    )
    return _ok("rebuild-proactive-context", policy.policy_id, version=policy.version)


@router.post("/reconcile-delivery", response_model=CommandResponse)
def reconcile_delivery(
    payload: ReconcileDeliveryPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """管理员确认 unknown Delivery，追加 canonical completed Event。"""
    from cogito.infrastructure.payload_store import PayloadStore
    from cogito.service.sqlite_delivery_service import SqliteDeliveryService

    gateway = getattr(deps.runtime, "gateway_client", None) if deps.runtime else None
    service = SqliteDeliveryService(
        deps.conn,
        gateway=gateway,
        effect_payload_store=PayloadStore(deps.config.resolve_payload_dir(), deps.conn),
    )
    delivery = service.get(payload.delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail=f"delivery {payload.delivery_id} not found")
    if delivery.status != "unknown":
        return _fail(
            "reconcile-delivery",
            payload.delivery_id,
            f"status is {delivery.status}, only unknown can be reconciled",
        )
    result = asyncio.run(
        service.reconcile(
            payload.delivery_id,
            delivery.platform_message_id,
            confirmed=True,
        )
    )
    if result.status != "sent":
        return _fail("reconcile-delivery", payload.delivery_id, result.status)
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="reconcile-delivery",
        target_type="delivery",
        target_id=payload.delivery_id,
        changes={"before": "unknown", "after": "sent", "event_sourced": True},
    )
    return _ok(
        "reconcile-delivery",
        payload.delivery_id,
        platform_message_id=result.platform_message_id,
    )


@router.post("/archive-skill", response_model=CommandResponse)
def archive_skill(
    payload: ArchiveSkillPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """归档 skill：skills 表 status → archived。"""
    from datetime import UTC, datetime

    # 优先 skills 表，其次 capabilities
    row = deps.conn.execute("SELECT * FROM skills WHERE skill_id=?", (payload.skill_id,)).fetchone()
    if row is None:
        row = deps.conn.execute(
            "SELECT * FROM capabilities WHERE capability_id=? OR name=?",
            (payload.skill_id, payload.skill_id),
        ).fetchone()
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
        deps.conn,
        actor_id=ACTOR,
        action="archive-skill",
        target_type="skill",
        target_id=payload.skill_id,
        changes={"before": before_status, "after": "archived"},
    )
    return _ok("archive-skill", payload.skill_id, before=before_status)


@router.post("/restore-skill", response_model=CommandResponse)
def restore_skill(
    payload: RestoreSkillPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """恢复 skill：status → active。"""
    from datetime import UTC, datetime

    row = deps.conn.execute("SELECT * FROM skills WHERE skill_id=?", (payload.skill_id,)).fetchone()
    if row is None:
        row = deps.conn.execute(
            "SELECT * FROM capabilities WHERE capability_id=? OR name=?",
            (payload.skill_id, payload.skill_id),
        ).fetchone()
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
        deps.conn,
        actor_id=ACTOR,
        action="restore-skill",
        target_type="skill",
        target_id=payload.skill_id,
        changes={"before": before_status, "after": "active"},
    )
    return _ok("restore-skill", payload.skill_id, before=before_status)


@router.post("/pin-skill", response_model=CommandResponse)
def pin_skill(
    payload: PinSkillPayload,
    deps: CommandDeps = Depends(get_command_deps),
) -> CommandResponse:
    """置顶/取消置顶 skill。"""
    from datetime import UTC, datetime

    row = deps.conn.execute("SELECT * FROM skills WHERE skill_id=?", (payload.skill_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"skill {payload.skill_id} not found in skills table"
        )
    before_pinned = bool(row["pinned"])
    deps.conn.execute(
        "UPDATE skills SET pinned=?, updated_at=? WHERE skill_id=?",
        (1 if payload.pinned else 0, datetime.now(UTC).isoformat(), payload.skill_id),
    )
    deps.conn.commit()
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="pin-skill",
        target_type="skill",
        target_id=payload.skill_id,
        changes={"before_pinned": before_pinned, "after_pinned": payload.pinned},
    )
    return _ok("pin-skill", payload.skill_id, pinned=payload.pinned)


@router.post("/disable-tool", response_model=CommandResponse)
def disable_tool(
    payload: DisableToolPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """禁用工具：更新 capabilities 表。"""
    # 幂等检查
    cached = _check_idempotency(deps.conn, ACTOR, "disable-tool", payload.idempotency_key)
    if cached:
        return cached
    # 读取当前状态用于 audit diff
    current = deps.conn.execute(
        "SELECT capability_id, disabled, health FROM capabilities "
        "WHERE capability_id=? OR tool_name=?",
        (payload.tool_name, payload.tool_name),
    ).fetchone()
    if current is None:
        return _fail("disable-tool", payload.tool_name, "tool not found in capabilities")
    before_disabled = bool(current["disabled"])
    deps.conn.execute(
        "UPDATE capabilities SET disabled=1, health='disabled' "
        "WHERE capability_id=? OR tool_name=?",
        (payload.tool_name, payload.tool_name),
    )
    deps.conn.commit()
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="disable-tool",
        target_type="tool",
        target_id=payload.tool_name,
        changes={
            "before_disabled": before_disabled,
            "after_disabled": True,
            "before_health": current["health"],
            "after_health": "disabled",
        },
    )
    return _ok("disable-tool", payload.tool_name, before_disabled=before_disabled)


@router.post("/create-backup", response_model=CommandResponse)
def create_backup(
    payload: CreateBackupPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """创建真实文件备份：复制 workspace → .workspace/backups/{ts}/。"""
    import shutil
    from datetime import UTC, datetime
    from pathlib import Path

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
            (
                backup_id,
                str(backup_dir),
                total_size / (1024 * 1024),
                datetime.now(UTC).isoformat(),
                "completed",
                "full",
            ),
        )
        deps.conn.commit()
        write_audit(
            deps.conn,
            actor_id=ACTOR,
            action="create-backup",
            target_type="backup",
            target_id=backup_id,
            changes={"path": str(backup_dir), "size_mb": total_size / (1024 * 1024)},
        )
        return _ok(
            "create-backup",
            backup_id,
            path=str(backup_dir),
            size_mb=round(total_size / (1024 * 1024), 2),
        )
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
def verify_backup(
    payload: VerifyBackupPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """验证备份：检查备份路径存在且包含 config.toml。"""
    from pathlib import Path

    row = deps.conn.execute(
        "SELECT * FROM backups WHERE backup_id=?", (payload.backup_id,)
    ).fetchone()
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
        deps.conn,
        actor_id=ACTOR,
        action="verify-backup",
        target_type="backup",
        target_id=payload.backup_id,
        changes={"verified": verified, "config_exists": config_exists, "data_exists": data_exists},
    )
    return _ok("verify-backup", payload.backup_id, verified=verified)


@router.post("/restore-backup", response_model=CommandResponse)
def restore_backup(
    payload: RestoreBackupPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """恢复备份：从备份路径复制回 workspace。"""
    import shutil
    from pathlib import Path

    cfg = deps.config
    row = deps.conn.execute(
        "SELECT * FROM backups WHERE backup_id=?", (payload.backup_id,)
    ).fetchone()
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
            deps.conn,
            actor_id=ACTOR,
            action="restore-backup",
            target_type="backup",
            target_id=payload.backup_id,
            changes={"restored": restored, "recovery_profile": True},
        )
        return _ok(
            "restore-backup",
            payload.backup_id,
            restored=restored,
            note="workspace restored; restart to apply",
        )
    except Exception as e:
        return _fail("restore-backup", payload.backup_id, str(e))


@router.post("/config-dry-run", response_model=CommandResponse)
def config_dry_run(
    payload: ConfigDryRunPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """配置 dry-run：校验 config 内容但不应用。"""
    import os
    import tempfile

    from cogito.config import Config

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
        deps.conn,
        actor_id=ACTOR,
        action="config-dry-run",
        target_type="config",
        target_id="dry-run",
        changes={"valid": result["valid"], "error_count": len(result["errors"])},
    )
    return _ok("config-dry-run", "dry-run", **result)


@router.post("/rollback-config", response_model=CommandResponse)
def rollback_config(
    payload: RollbackConfigPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """配置回滚：从 config_versions 读取历史版本的 content 并写回 config.toml。"""
    from datetime import UTC, datetime

    from cogito.config import ConfigVersionRepository

    repo = ConfigVersionRepository(deps.conn)
    ver = repo.get(payload.version_id)
    if ver is None:
        raise HTTPException(
            status_code=404, detail=f"config version {payload.version_id} not found"
        )
    # 最新 active version
    latest = repo.latest()
    if latest and latest.content_hash == ver.content_hash:
        return _ok("rollback-config", payload.version_id, note="already at this version")
    # 插入新版本（回滚也是一个新版本）
    new_version_id = __import__("uuid").uuid4().hex
    deps.conn.execute(
        "INSERT INTO config_versions "
        "(version_id, content_hash, schema_version, source_layers, "
        "applied_at, change_summary) "
        "VALUES (?,?,?,?,?,?)",
        (
            new_version_id,
            ver.content_hash,
            ver.schema_version,
            __import__("json").dumps(ver.source_layers + ["rollback"]),
            int(datetime.now(UTC).timestamp() * 1000),
            f"rollback to {payload.version_id}",
        ),
    )
    deps.conn.commit()
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="rollback-config",
        target_type="config",
        target_id=new_version_id,
        changes={"from_version": payload.version_id, "to_hash": ver.content_hash},
    )
    return _ok(
        "rollback-config",
        new_version_id,
        restored_hash=ver.content_hash,
        note="config_versions updated; actual config.toml restore requires file system write",
    )


@router.post("/payload-gc-dry-run", response_model=CommandResponse)
def payload_gc_dry_run(
    payload: PayloadGcDryRunPayload, deps: CommandDeps = Depends(get_command_deps)
) -> CommandResponse:
    """Payload GC dry-run：列出可回收的孤立对象。"""
    orphans = deps.conn.execute(
        "SELECT payload_ref, size FROM payload_objects WHERE payload_ref NOT IN "
        "(SELECT content_ref FROM deliveries WHERE content_ref IS NOT NULL) "
        "AND payload_ref NOT IN "
        "(SELECT payload_ref FROM event_log WHERE payload_ref IS NOT NULL) "
        "LIMIT 200"
    ).fetchall()
    orphan_refs = [r["payload_ref"] for r in orphans]
    total_size = sum(r["size"] for r in orphans)
    write_audit(
        deps.conn,
        actor_id=ACTOR,
        action="payload-gc-dry-run",
        target_type="payload",
        target_id="orphans",
        changes={"orphan_count": len(orphan_refs), "total_size_bytes": total_size},
    )
    return _ok(
        "payload-gc-dry-run",
        "orphans",
        orphan_count=len(orphan_refs),
        total_size_bytes=total_size,
        sample=orphan_refs[:10],
    )
