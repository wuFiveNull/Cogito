"""RecoveryDecision + RecoveryAdvisor + Checkpoint — Plan 02 M2."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cogito.domain.turn import RunAttempt, RunAttemptStatus, Turn, TurnStatus
from cogito.service.recovery_decision import (
    Checkpoint,
    RecoveryAdvisor,
    RecoveryDecision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _turn(status: TurnStatus = TurnStatus.running, **kw: Any) -> Turn:
    return Turn(turn_id="t1", status=status, **kw)


def _attempt(
    status: RunAttemptStatus = RunAttemptStatus.running,
    lease_expires_at: datetime | None = None,
    **kw: Any,
) -> RunAttempt:
    return RunAttempt(
        attempt_id="a1", turn_id="t1", status=status,
        lease_expires_at=lease_expires_at, **kw,
    )


def advisor() -> RecoveryAdvisor:
    return RecoveryAdvisor(config_version="1.0", capability_snapshot_version="1.0")


# ---------------------------------------------------------------------------
# 1. Cancelled Turn → fail
# ---------------------------------------------------------------------------


def test_cancelled_turn_fails() -> None:
    ev = advisor().decide(_turn(TurnStatus.cancelled), _attempt())
    assert ev.decision == RecoveryDecision.fail
    assert ev.reason_code == "turn_cancelled"


# ---------------------------------------------------------------------------
# 2. side_effect_unknown → reconcile (highest priority after cancel)
# ---------------------------------------------------------------------------


def test_unknown_side_effect_reconciles() -> None:
    ck = Checkpoint(
        checkpoint_id="ck1", turn_id="t1", attempt_id="a1",
        tool_calls=[
            {"id": "tc1", "status": "succeeded", "receipt_ref": "r1"},
            {"id": "tc2", "status": "unknown", "receipt_ref": None},
        ],
    )
    ev = advisor().decide(_turn(), _attempt(), ck)
    assert ev.decision == RecoveryDecision.reconcile
    assert ev.reason_code == "side_effect_unknown"
    assert len(ev.decision_evidence["unknown_tool_calls"]) == 1


# ---------------------------------------------------------------------------
# 3. waiting_user / waiting_external → waiting_user
# ---------------------------------------------------------------------------


def test_waiting_user_stays_waiting() -> None:
    ev = advisor().decide(_turn(TurnStatus.waiting_user), _attempt())
    assert ev.decision == RecoveryDecision.waiting_user
    assert ev.reason_code == "turn_waiting_user"


def test_pending_approval_waits() -> None:
    ck = Checkpoint(pending_approval_id="ap1")
    ev = advisor().decide(_turn(), _attempt(), ck)
    assert ev.decision == RecoveryDecision.waiting_user
    assert ev.reason_code == "pending_approval"


# ---------------------------------------------------------------------------
# 4. Lease expired + checkpoint → resume (after compat check)
# ---------------------------------------------------------------------------


def test_lease_expired_with_checkpoint_resumes() -> None:
    expired = datetime.now(UTC) - timedelta(seconds=60)
    ck = Checkpoint(checkpoint_id="ck1")
    ev = advisor().decide(_turn(), _attempt(lease_expires_at=expired), ck)
    assert ev.decision == RecoveryDecision.resume
    assert ev.decision_evidence["checkpoint_id"] == "ck1"


# ---------------------------------------------------------------------------
# 5. Lease expired + no checkpoint → retry
# ---------------------------------------------------------------------------


def test_lease_expired_no_checkpoint_retries() -> None:
    expired = datetime.now(UTC) - timedelta(seconds=60)
    ev = advisor().decide(_turn(), _attempt(lease_expires_at=expired))
    assert ev.decision == RecoveryDecision.retry
    assert ev.reason_code == "lease_expired_no_checkpoint"


# ---------------------------------------------------------------------------
# 6. Config version mismatch → manual_review
# ---------------------------------------------------------------------------


def test_config_mismatch_manual_review() -> None:
    expired = datetime.now(UTC) - timedelta(seconds=60)
    ck = Checkpoint(checkpoint_id="ck1", config_version="0.5")  # != runtime 1.0
    ev = advisor().decide(_turn(), _attempt(lease_expires_at=expired), ck)
    assert ev.decision == RecoveryDecision.manual_review
    assert ev.reason_code == "config_version_mismatch"


def test_capability_mismatch_manual_review() -> None:
    expired = datetime.now(UTC) - timedelta(seconds=60)
    ck = Checkpoint(checkpoint_id="ck1", capability_snapshot_version="0.5")
    ev = advisor().decide(_turn(), _attempt(lease_expires_at=expired), ck)
    assert ev.decision == RecoveryDecision.manual_review
    assert ev.reason_code == "capability_snapshot_mismatch"


# ---------------------------------------------------------------------------
# 7. Parent attempt is always recorded in evidence
# ---------------------------------------------------------------------------


def test_parent_attempt_recorded() -> None:
    ev = advisor().decide(_turn(TurnStatus.cancelled), _attempt())
    assert ev.parent_attempt_id == "a1"


# ---------------------------------------------------------------------------
# 8. Checkpoint round-trip (13 fields, no SDK/stack leakage)
# ---------------------------------------------------------------------------


def test_checkpoint_roundtrip() -> None:
    ck = Checkpoint(
        checkpoint_id="ck1", turn_id="t1", attempt_id="a1",
        current_step="tool_call",
        completed_step_ids=["context", "model_call"],
        tool_calls=[{"id": "tc1", "status": "succeeded", "receipt_ref": "r1"}],
        budget_consumed={"tokens": 100, "cost": 0.01},
    )
    data = ck.to_dict()
    restored = Checkpoint.from_dict(data)
    assert restored.checkpoint_id == "ck1"
    assert restored.current_step == "tool_call"
    assert restored.completed_step_ids == ["context", "model_call"]
    assert restored.budget_consumed == {"tokens": 100, "cost": 0.01}


def test_checkpoint_forbids_mutable_fields() -> None:
    """Checkpoint fields are data-only by design — frozen dataclass."""
    ck = Checkpoint(checkpoint_id="ck1")
    with pytest.raises(AttributeError):
        ck.turn_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 9. RecoveryDecision enum is complete
# ---------------------------------------------------------------------------


def test_recovery_decision_values() -> None:
    assert {d.value for d in RecoveryDecision} == {
        "resume", "retry", "reconcile", "waiting_user", "manual_review", "fail",
    }
