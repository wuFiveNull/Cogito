"""CommandService —— 命令可写转写的薄服务层。

涵盖现有领域服务未覆盖的状态转写：审批 Event、投递重放 (deliveries)。
handler 调用此处方法，不直接执行 SQL —— 遵守"增删改查一律走服务"。
"""

from __future__ import annotations

import sqlite3

from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.turn import TurnStatus
from cogito.service.approval_service import (
    ApprovalNotFoundError,
    ApprovalStateError,
    SqliteApprovalService,
)
from cogito.store.event_store import EventStore
from cogito.store.repositories import TurnRepository


def set_approval_decision(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
    decision: str,  # 'approved' | 'rejected'
    responder_id: str,
    expected_version: int | None = None,
    action_hash: str = "",
    response_reason: str = "",
) -> bool:
    """写入审批决定。返回 False 表示审批不存在或非 pending。"""
    # response_reason is audit-only; raw response text is deliberately absent
    # from immutable Events.  The aggregate validates replayed state/version.
    service = SqliteApprovalService(conn)
    try:
        if decision == "approved":
            service.approve(
                approval_id, responder_id,
                expected_version=expected_version, action_hash=action_hash,
            )
        elif decision == "rejected":
            service.reject(approval_id, responder_id, expected_version=expected_version)
        else:
            return False
    except (ApprovalNotFoundError, ApprovalStateError):
        return False
    return True


def resume_turn_after_approval(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
) -> str | None:
    """审批消费后把关联 Turn 从 waiting_user → queued（仅一次）。

    返回 turn_id 表示成功；None 表示 approval 不存在/非 pending/无关联 Turn。
    幂等：重复消费同一 approved approval 不产生第二个 queued 状态。
    """
    approval = SqliteApprovalService(conn).get(approval_id)
    if approval is None or approval["status"] != "approved":
        return None
    turn_id = str(approval["turn_id"] or "")
    if not turn_id:
        return None

    # Read turn state from Event stream
    from cogito.store.event_replay import replay_turn

    turn_stream = EventStore(conn).read_stream("turn", turn_id)
    turn_state = replay_turn(turn_stream, turn_id)
    if turn_state is None or turn_state.status not in {"waiting_user", "waiting_external"}:
        return None  # 已消费或状态不对
    approval_stream = EventStore(conn).read_stream("approval", approval_id)
    approval_event = approval_stream[-1] if approval_stream else None
    approval_context = approval_event.context if approval_event else EventContext()
    resumed = TurnRepository(conn).update_status(
        turn_id,
        TurnStatus.queued,
        expected_version=turn_state.stream_version,
        event_context=EventContext(
            trace_id=approval_context.trace_id or turn_id,
            correlation_id=approval_context.correlation_id or turn_id,
            causation_id=approval_event.event_id if approval_event else approval_context.causation_id,
            principal_id=approval_context.principal_id,
            session_id=turn_state.session_id or "",
            turn_id=turn_id,
        ),
        event_producer="approval-command",
    )
    if not resumed:
        return None
    # Child-Agent approvals resume their durable Task as well as the child Turn.
    child_links = conn.execute(
        "SELECT task_id FROM child_task_links WHERE turn_id=?",
        (turn_id,),
    ).fetchall()
    task_ids = [str(r["task_id"]) for r in child_links]
    store = EventStore(conn)
    for task_id in task_ids:
        task = TaskRepository(conn).get(task_id)
        if task is None or task.status.value != "waiting_user":
            continue
        store.append(
            Event(
                event_type="task.scheduled",
                stream_type="task",
                stream_id=task_id,
                producer="approval-command",
                event_class=EventClass.DOMAIN,
                context=EventContext(
                    trace_id=approval_context.trace_id or turn_id,
                    correlation_id=approval_context.correlation_id or turn_id,
                    causation_id=approval_event.event_id if approval_event else "",
                    principal_id=approval_context.principal_id,
                    turn_id=turn_id,
                    task_id=task_id,
                ),
                summary="Task resumed after approval",
                attributes={"reason": "approval_approved", "approval_id": approval_id},
                outcome="queued",
                idempotency_key=f"task:{task_id}:approval-resume:{approval_id}",
            )
        )
    return turn_id
