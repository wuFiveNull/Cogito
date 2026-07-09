"""PR-I4: Command Envelope + Control Plane — Plan 05 M4."""
from __future__ import annotations

import pytest

from cogito.interaction_web.command_envelope import (
    CommandEnvelope,
    CommandResult,
    CommandError,
    IdempotencyConflictError,
    VersionConflictError,
    enforce_loopback_only,
    resolve_conflict,
)


def test_command_envelope_auto_id() -> None:
    cmd = CommandEnvelope(command_type="approve", actor="owner")
    assert cmd.command_id != ""


def test_conflict_priority_terminal_wins() -> None:
    """终态 > 取消/过期 > 审批 > 重试。returns True = current wins (blocks incoming)。"""
    # completed 不能被 retrying 覆盖 → current wins
    assert resolve_conflict("completed", "retrying") is True
    # approved vs cancelled → cancelled 优先级更高 → current(approved) 不赢
    assert resolve_conflict("approved", "cancelled") is False
    # retrying 不能覆盖 completed → current(completed) wins
    assert resolve_conflict("completed", "retrying") is True


def test_loopback_only_blocks_remote() -> None:
    """默认仅 loopback。"""
    enforce_loopback_only("127.0.0.1")  # OK
    with pytest.raises(CommandError):
        enforce_loopback_only("192.168.1.1")


def test_idempotency_conflict_type() -> None:
    """幂等冲突有独立异常类型。"""
    err = IdempotencyConflictError("dup")
    assert isinstance(err, CommandError)


def test_version_conflict_type() -> None:
    """版本冲突有独立异常类型。"""
    err = VersionConflictError("stale")
    assert isinstance(err, CommandError)


def test_command_result_status() -> None:
    """CommandResult 包含完整状态。"""
    r = CommandResult(command_id="c1", status="ok", target_id="t1", previous_version=3)
    assert r.status == "ok"
    assert r.previous_version == 3
