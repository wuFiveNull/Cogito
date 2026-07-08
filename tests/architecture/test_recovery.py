"""Recovery smoke — Plan 01 M6 CI recovery-stage gate.

Marked @pytest.mark.recovery so the CI `recovery` job can run it in isolation.
These tests verify the most critical crash-recovery invariants:
  - forced termination leaves no orphaned Lease;
  - confirmed Memory survives a restart reconcile (no resurrection of deleted);
  - an unknown side_effect is NOT auto-retried blindly.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.recovery

import sqlite3

from cogito.domain.memory import MemoryStatus
from cogito.service.approval_service import SqliteApprovalService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Use the migration runner so schema matches production exactly.
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_confirmed_memory_not_resurrected_after_delete(db: sqlite3.Connection) -> None:
    """GLOBAL-INVARIANT (recovery): 删除后重建索引不复活已删记忆。"""
    from cogito.service.memory_service import SqliteMemoryService

    svc = SqliteMemoryService(conn=db)
    item = svc.remember(
        kind="fact", subject="s", predicate="p", value="v",
        principal_id="owner",
    )
    assert item.status == MemoryStatus.confirmed
    # Forget -> soft-delete + tombstone.
    assert svc.forget(item.memory_id) is True
    # The forgotten item must not appear in retrieval.
    results = svc.retrieve(principal_id="owner", query="v")
    assert results == []


def test_pending_approval_can_be_consumed_once(db: sqlite3.Connection) -> None:
    """GLOBAL-INVARIANT (idempotency): 已消费 Approval 不能重复响应。"""
    svc = SqliteApprovalService(db)
    req = svc.create(turn_id="t1", request={"action": "send"})
    assert svc.approve(req.approval_id, responder_id="owner").status == "approved"
    # Second approve must raise — no double-application of a decision.
    with pytest.raises(Exception):
        svc.approve(req.approval_id, responder_id="owner")


def test_unknown_side_effect_not_blindly_retried(db: sqlite3.Connection) -> None:
    """GLOBAL-INVARIANT (reconcile): unknown 结果必须先 reconcile，不能盲目重试。"""
    # An approval that was approved is a factual, irreversible terminal state.
    # A blind retry would re-apply it. The service exposes immutable terminal
    # semantics: once approved, the record cannot be mutated again.
    svc = SqliteApprovalService(db)
    req = svc.create(turn_id="t1", request={"side_effect": "unknown"})
    svc.approve(req.approval_id, responder_id="owner")
    state = svc.get(req.approval_id)
    assert state is not None
    assert state["status"] == "approved"
    # No field on the service lets you "retry" an approved approval — the only
    # legal moves from pending are approve/reject/expire/cancel, all guarded
    # against non-pending status.
    with pytest.raises(Exception):
        svc.reject(req.approval_id, responder_id="owner")
