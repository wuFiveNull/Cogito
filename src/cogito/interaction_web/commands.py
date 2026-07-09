"""Command API 路由 —— 可写命令。

ACCESS-DELIVERY §2.3。所有命令：
  - 接受幂等键 + 生成 command_id
  - 经过服务层 (Dispatcher / TaskRepository / SqliteMemoryService / ConnectorRepository ...)
  - 写入 audit_records
  - DB 增删改查一律走服务；handler 不直写 SQL (audit.py 的 write_audit 除外)。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from cogito.interaction_web.audit import write_audit
from cogito.interaction_web.command_service import (
    replay_delivery,
    resume_turn_after_approval,
    set_approval_decision,
)
from cogito.interaction_web.deps import CommandDeps, get_command_deps
from cogito.interaction_web.models import (
    ApprovalPayload,
    CancelTurnPayload,
    CommandResponse,
    ConfigDryRunPayload,
    CreateBackupPayload,
    DeleteSessionPayload,
    DeleteSessionsByConvPayload,
    DisablePluginPayload,
    DisableToolPayload,
    MemoryConfirmPayload,
    MemoryDeletePayload,
    PauseConnectorPayload,
    PayloadGcDryRunPayload,
    ReconcileReceiptPayload,
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
    deps.conn.commit()
    write_audit(
        deps.conn, actor_id=ACTOR, action="confirm-memory",
        target_type="memory", target_id=payload.memory_id,
    )
    return _ok("confirm-memory", payload.memory_id)


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


@router.post("/delete-session", response_model=CommandResponse)
def delete_session(payload: DeleteSessionPayload, deps: CommandDeps = Depends(get_command_deps)) -> CommandResponse:
    """软删除会话：设置 deleted_at 时间戳，数据保留但页面不再显示。"""
    row = deps.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id=? AND deleted_at IS NULL",
        (payload.session_id,),
    ).fetchone()
    if row is None:
        return _fail("delete-session", payload.session_id, "not found or already deleted")
    from datetime import UTC, datetime
    deleted_at = datetime.now(UTC).isoformat()
    deps.conn.execute(
        "UPDATE sessions SET deleted_at=? WHERE session_id=?",
        (deleted_at, payload.session_id),
    )
    deps.conn.commit()
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
        "SELECT session_id FROM sessions WHERE conversation_id=? AND deleted_at IS NULL",
        (payload.conversation_id,),
    ).fetchall()
    if not rows:
        return _fail("delete-sessions-by-conversation", payload.conversation_id, "no active sessions")
    deleted_at = datetime.now(UTC).isoformat()
    ids = [r["session_id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    deps.conn.execute(
        f"UPDATE sessions SET deleted_at=? WHERE session_id IN ({placeholders})",
        [deleted_at, *ids],
    )
    deps.conn.commit()
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
    """禁用插件：写入审计 (运行时配置修改由 config 管理，仅记录意图)。"""
    write_audit(
        deps.conn, actor_id=ACTOR, action="disable-plugin",
        target_type="plugin", target_id=payload.name,
    )
    return _ok("disable-plugin", payload.name)


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
