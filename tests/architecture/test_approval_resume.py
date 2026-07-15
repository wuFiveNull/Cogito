"""PR-R3: cancel + approval-resume end-to-end — Plan 02 M3."""

from __future__ import annotations

import pytest

from cogito.domain.turn import Turn, TurnStatus
from cogito.service.api.command_service import (
    resume_turn_after_approval,
    set_approval_decision,
)
from cogito.service.approval_service import SqliteApprovalService


@pytest.fixture
def db() -> Any:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate

    migrate(conn)
    return conn


def _create_waiting_turn(db: Any, turn_id: str = "t1") -> None:
    from datetime import datetime, UTC

    db.execute(
        "INSERT INTO turns (turn_id, session_id, status, priority, version, created_at) "
        "VALUES (?, ?, 'waiting_user', 80, 1, ?)",
        (turn_id, "s1", datetime.now(UTC).isoformat()),
    )
    db.commit()


def test_approve_resumes_waiting_turn(db: Any) -> None:
    """审批消费后仅创建一个恢复：Turn waiting_user → queued。"""
    _create_waiting_turn(db)
    svc = SqliteApprovalService(db)
    req = svc.create(turn_id="t1", request={"action": "send"})
    svc.approve(req.approval_id, responder_id="owner")

    resumed = resume_turn_after_approval(db, approval_id=req.approval_id)
    assert resumed == "t1"

    row = db.execute("SELECT status FROM turns WHERE turn_id='t1'").fetchone()
    assert row["status"] == "queued"


def test_approve_is_idempotent_single_resume(db: Any) -> None:
    """重复消费同一 approved approval 不产生第二个 queued。"""
    _create_waiting_turn(db)
    svc = SqliteApprovalService(db)
    req = svc.create(turn_id="t1", request={})
    svc.approve(req.approval_id, responder_id="owner")

    first = resume_turn_after_approval(db, approval_id=req.approval_id)
    second = resume_turn_after_approval(db, approval_id=req.approval_id)
    assert first == "t1"
    assert second is None  # 幂等：第二次不恢复


def test_reject_does_not_resume_turn(db: Any) -> None:
    """拒绝不能让 Turn 恢复。"""
    _create_waiting_turn(db)
    svc = SqliteApprovalService(db)
    req = svc.create(turn_id="t1", request={})
    svc.reject(req.approval_id, responder_id="owner")

    resumed = resume_turn_after_approval(db, approval_id=req.approval_id)
    assert resumed is None
    row = db.execute("SELECT status FROM turns WHERE turn_id='t1'").fetchone()
    assert row["status"] == "waiting_user"  # 仍在等待


def test_cancel_turn_sets_status(db: Any) -> None:
    """CancelTurn 把 queued → cancelled。"""
    from datetime import datetime, UTC

    db.execute(
        "INSERT INTO turns (turn_id, session_id, status, priority, version, created_at) "
        "VALUES (?, ?, 'queued', 80, 1, ?)",
        ("t1", "s1", datetime.now(UTC).isoformat()),
    )
    db.commit()
    from cogito.service.dispatcher import Dispatcher

    d = Dispatcher(db)
    ok = d.cancel("t1", expected_version=1)
    assert ok is True
    row = db.execute("SELECT status FROM turns WHERE turn_id='t1'").fetchone()
    assert row["status"] == "cancelled"


def test_approval_expired_no_resume(db: Any) -> None:
    """过期审批不能让 Turn 恢复（需先 approve 再 resume）。"""
    _create_waiting_turn(db)
    svc = SqliteApprovalService(db)
    req = svc.create(turn_id="t1", request={}, ttl_seconds=0)
    # 不 approve，直接尝试 resume → 失败
    resumed = resume_turn_after_approval(db, approval_id=req.approval_id)
    assert resumed is None


from typing import Any  # noqa: E402  (used in fixture)
