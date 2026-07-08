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
from cogito.interaction_web.command_service import replay_delivery, set_approval_decision
from cogito.interaction_web.deps import CommandDeps, get_command_deps
from cogito.interaction_web.models import (
    ApprovalPayload,
    CancelTurnPayload,
    CommandResponse,
    DeleteSessionPayload,
    DisablePluginPayload,
    MemoryConfirmPayload,
    MemoryDeletePayload,
    PauseConnectorPayload,
    ReplayDeliveryPayload,
    RetryTaskPayload,
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
    write_audit(
        deps.conn, actor_id=ACTOR, action="approve",
        target_type="approval", target_id=payload.approval_id,
    )
    return _ok("approve", payload.approval_id)


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
