"""Facade / public-face tests — Plan 01 M2.

Verifies every SYSTEM-BOUNDARIES / 4 aggregate has a unique write entry
(Protocol) defined in cogito.service, and that implementations exist for
the previously-undefined ones (Task, Approval, IdentityConversation).
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from cogito.service.approval_service import SqliteApprovalService
from cogito.service.identity_service import SqliteIdentityConversationService
from cogito.service.task_service import TaskService, SqliteTaskService


# ---------------------------------------------------------------------------
# 1. Each state owner points to a unique Protocol + concrete implementation.
# ---------------------------------------------------------------------------

PROTOCOL_CONCRETE = {
    "turn": ("cogito.service.turn_service", "TurnService"),
    "task": ("cogito.service.task_service", "TaskService"),
    "memory": ("cogito.service.memory_service", "MemoryService"),
    "delivery": ("cogito.service.delivery_service", "DeliveryService"),
    "approval": ("cogito.service.approval_service", "ApprovalService"),
    "identity": ("cogito.service.identity_service", "IdentityConversationService"),
    "plugin": ("cogito.service.plugin_runtime", "PluginRuntime"),
}


def test_each_aggregate_has_protocol_defined() -> None:
    """Every SYSTEM-BOUNDARIES / 4 owner maps to a Protocol class."""
    for name, (mod_path, cls_name) in PROTOCOL_CONCRETE.items():
        mod = __import__(mod_path, fromlist=[cls_name])
        cls = getattr(mod, cls_name)
        assert inspect.isclass(cls), f"{name} Protocol {cls_name} missing in {mod_path}"


def test_previously_missing_facades_have_implementations() -> None:
    """Task/Approval/Identity facades ship a concrete class."""
    conn = sqlite3.connect(":memory:")
    # Task service is constructible and satisfies the Protocol structurally.
    svc = SqliteTaskService(conn)
    assert hasattr(svc, "create")
    assert hasattr(svc, "claim")
    assert hasattr(svc, "complete")

    # Approval service is constructible and exposes the write entry points.
    apv = SqliteApprovalService(conn)
    assert hasattr(apv, "create")
    assert hasattr(apv, "approve")
    assert hasattr(apv, "reject")

    # Identity service is constructible.
    ident = SqliteIdentityConversationService(conn)
    assert hasattr(ident, "resolve_identity")
    assert hasattr(ident, "resolve_conversation")
    assert hasattr(ident, "resolve_session")


# ---------------------------------------------------------------------------
# 2. Approval facade: the unique write entry replaces direct SQL in commands.
# ---------------------------------------------------------------------------


def _mem_db_with_approvals() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE approvals ("
        "approval_id TEXT PRIMARY KEY, turn_id TEXT, request TEXT NOT NULL DEFAULT '{}', "
        "status TEXT NOT NULL DEFAULT 'pending', responder_id TEXT, decided_at TEXT, "
        "expires_at TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    return conn


def test_approval_create_then_approve() -> None:
    conn = _mem_db_with_approvals()
    svc = SqliteApprovalService(conn)
    req = svc.create(turn_id="t1", request={"action": "send"})
    assert req.approval_id
    assert req.expires_at
    dec = svc.approve(req.approval_id, responder_id="owner")
    assert dec.status == "approved"
    assert dec.responder_id == "owner"


def test_approval_double_approve_raises() -> None:
    conn = _mem_db_with_approvals()
    svc = SqliteApprovalService(conn)
    req = svc.create(turn_id="t1", request={})
    svc.approve(req.approval_id, responder_id="owner")
    with pytest.raises(Exception):
        svc.approve(req.approval_id, responder_id="owner")


def test_approval_get_returns_record() -> None:
    conn = _mem_db_with_approvals()
    svc = SqliteApprovalService(conn)
    req = svc.create(turn_id="t1", request={"x": 1})
    row = svc.get(req.approval_id)
    assert row is not None
    assert row["status"] == "pending"
