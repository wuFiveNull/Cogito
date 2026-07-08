"""Query API 路由 —— 只读查询。

ACCESS-DELIVERY §2.2。所有读请求经 QueryService，handler 不直接执行 SQL。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from cogito.interaction_web.deps import CommandDeps, get_command_deps
from cogito.interaction_web.models import Pagination
from cogito.interaction_web.query_service import QueryService

router = APIRouter(prefix="/api", tags=["query"])


def _svc(deps: CommandDeps) -> QueryService:
    return QueryService(deps.conn, deps.config)


# ── status / usage ────────────────────────────────────────────


@router.get("/status")
def status(deps: CommandDeps = Depends(get_command_deps)) -> dict:
    return _svc(deps).status(recovery_counts=deps.recovery_counts)


@router.get("/usage")
def usage(
    hours: int = Query(24, ge=1, le=720),
    deps: CommandDeps = Depends(get_command_deps),
) -> dict:
    return _svc(deps).usage(hours=hours)


# ── turns ─────────────────────────────────────────────────────


@router.get("/turns")
def list_turns(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    deps: CommandDeps = Depends(get_command_deps),
) -> Pagination:
    return _svc(deps).list_turns(status=status, limit=limit, offset=offset)


@router.get("/turns/{turn_id}")
def get_turn(turn_id: str, deps: CommandDeps = Depends(get_command_deps)) -> dict:
    out = _svc(deps).get_turn(turn_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"turn {turn_id} not found")
    return out


@router.get("/turns/{turn_id}/attempts")
def get_turn_attempts(turn_id: str, deps: CommandDeps = Depends(get_command_deps)) -> dict:
    out = _svc(deps).get_turn(turn_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"turn {turn_id} not found")
    return {"turn_id": turn_id, "attempts": out["attempts"]}


# ── tasks ─────────────────────────────────────────────────────


@router.get("/tasks")
def list_tasks(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    deps: CommandDeps = Depends(get_command_deps),
) -> Pagination:
    return _svc(deps).list_tasks(status=status, limit=limit, offset=offset)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, deps: CommandDeps = Depends(get_command_deps)) -> dict:
    out = _svc(deps).get_task(task_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return out


# ── memory ────────────────────────────────────────────────────


@router.get("/memory")
def search_memory(
    q: str = "",
    limit: int = Query(50, ge=1, le=200),
    deps: CommandDeps = Depends(get_command_deps),
) -> dict:
    return _svc(deps).search_memory(q=q, limit=limit)


# ── connectors / channels / conversations ─────────────────────


@router.get("/connectors")
def list_connectors(deps: CommandDeps = Depends(get_command_deps)) -> dict:
    return _svc(deps).list_connectors()


@router.get("/channels")
def list_channels(deps: CommandDeps = Depends(get_command_deps)) -> dict:
    return _svc(deps).list_channels()


@router.get("/conversations")
def list_conversations(
    limit: int = Query(100, ge=1, le=500),
    deps: CommandDeps = Depends(get_command_deps),
) -> dict:
    return _svc(deps).list_conversations(limit=limit)


@router.get("/conversations/{conversation_id}/messages")
def get_conversation_messages(
    conversation_id: str,
    limit: int = Query(200, ge=1, le=1000),
    deps: CommandDeps = Depends(get_command_deps),
) -> dict:
    return _svc(deps).get_conversation_messages(conversation_id, limit=limit)


# ── sessions ──────────────────────────────────────────────────


@router.get("/sessions")
def list_sessions(
    limit: int = Query(100, ge=1, le=500),
    deps: CommandDeps = Depends(get_command_deps),
) -> dict:
    return _svc(deps).list_sessions(limit=limit)


@router.get("/sessions/{session_id}/trace")
def get_session_trace(session_id: str, deps: CommandDeps = Depends(get_command_deps)) -> dict:
    out = _svc(deps).get_session_trace(session_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    return out


# ── deliveries / traces / plugins ─────────────────────────────


@router.get("/deliveries")
def list_deliveries(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    deps: CommandDeps = Depends(get_command_deps),
) -> dict:
    return _svc(deps).list_deliveries(status=status, limit=limit)


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, deps: CommandDeps = Depends(get_command_deps)) -> dict:
    out = _svc(deps).get_trace(trace_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
    return out


@router.get("/plugins")
def list_plugins(deps: CommandDeps = Depends(get_command_deps)) -> dict:
    return _svc(deps).list_plugins()
