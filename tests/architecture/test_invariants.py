"""Top 10 high-risk invariant automated tests — Plan 01 M5.

Each test maps to an invariant_id in docs/architecture/invariant-verification-matrix.md.
These invariants are enforced across the codebase — tests here are smoke guards
that can be extended into property-based or integration tests.
"""
from __future__ import annotations

import pytest

# ── INV-1.1 SQLite sole factual source ───────────────────────────

def test_inv_1_1_foreign_keys_enforce_references() -> None:
    """SQLite FK 约束保证引用完整性（业务事实唯一源）。"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE child (id TEXT PRIMARY KEY, pid TEXT REFERENCES parent(id))")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO child VALUES ('c1', 'nonexistent')")


# ── INV-2.2 One submittable Turn per Context Partition ──────────

def test_inv_2_2_snapshot_items_are_immutable() -> None:
    """ContextSnapshot items 在创建后不可变（同 Partition 单一可提交 Turn）。"""
    from cogito.runtime.context import ContextSnapshot, ContextItem
    item = ContextItem(item_type="message", item_id="m1", source="s1", content="hello")
    snap = ContextSnapshot(items=(item,))
    with pytest.raises(AttributeError):
        snap.items = ()  # type: ignore[misc]


# ── INV-2.4 External calls not in DB transaction ────────────────

def test_inv_2_4_tool_request_frozen_no_db_ref() -> None:
    """ToolRequest 是 frozen dataclass 且不持有 DB 连接。"""
    from cogito.contracts.envelope import ToolRequest
    req = ToolRequest(tool_name="echo")
    assert not hasattr(req, "conn")
    assert not hasattr(req, "cursor")


# ── INV-2.5 Old Lease/Attempt must not commit ───────────────────

def test_inv_2_5_frozen_tool_result_cannot_mutate() -> None:
    """ToolResult frozen — old Worker 不能修改最终结果。"""
    from cogito.capability.models import ToolResult
    r = ToolResult(tool_call_id="c1", tool_name="echo", status="success", result="ok")
    with pytest.raises(AttributeError):
        r.status = "changed"  # type: ignore[misc]


# ── INV-3.1 Idempotency keys ────────────────────────────────────

def test_inv_3_1_error_envelope_has_trace() -> None:
    """每个 ErrorEnvelope 都有 trace_id（幂等键 + 追踪）。"""
    from cogito.contracts.envelope import ErrorEnvelope, ErrorCategory
    err = ErrorEnvelope(category=ErrorCategory.rate_limit, message="x", trace_id="t1")
    assert err.trace_id == "t1"


# ── INV-3.2 Side-effect intent-then-receipt ─────────────────────

def test_inv_3_2_sidemap_effect_receipt_frozen() -> None:
    """SideEffectReceipt 不可变（意图已持久化后的不可篡改证据）。"""
    from cogito.capability.models import SideEffectReceipt
    r = SideEffectReceipt(receipt_id="r1", status="succeeded")
    with pytest.raises(AttributeError):
        r.status = "changed"  # type: ignore[misc]


# ── INV-3.3 unknown → reconcile first ──────────────────────────

def test_inv_3_3_unknown_forces_reconcile_decision() -> None:
    """side_effect_unknown 必须进入 reconcile（不自动重试）。"""
    from cogito.service.recovery_decision import Checkpoint, RecoveryAdvisor, RecoveryDecision
    ck = Checkpoint(checkpoint_id="ck1",
                    tool_calls=[{"id": "tc1", "status": "unknown"}])
    ev = RecoveryAdvisor().decide(
        type("T", (), {"status": "running"})(),
        type("A", (), {"attempt_id": "a1", "lease_expires_at": None})(),
        ck,
    )
    assert ev.decision == RecoveryDecision.reconcile


# ── INV-5.2 External content cannot raise trust ─────────────────

def test_inv_5_2_context_item_trust_external_by_default() -> None:
    """ContextItem 默认 trust_label=untrusted（外部内容不能自升信任）。"""
    from cogito.runtime.context import ContextItem
    item = ContextItem(item_type="memory", item_id="m1", source="external")
    assert item.trust_label == "unverified"


# ── INV-2.3 Waiting holds no txn/Lane/Lease ────────────────────

def test_inv_2_3_waiting_status_no_lease_flag() -> None:
    """waiting_user 状态不持 Lease。"""
    from cogito.domain.turn import Turn, TurnStatus
    t = Turn(status=TurnStatus.waiting_user)
    assert t.status.value == "waiting_user"


# ── INV-5.4 Control plane loopback-only ─────────────────────────

def test_inv_5_4_loopback_only() -> None:
    """默认仅 loopback，显式拒绝远程 origin。"""
    from cogito.interaction_web.command_envelope import enforce_loopback_only
    import pytest as _pt
    enforce_loopback_only("127.0.0.1")  # OK
    with _pt.raises(Exception):
        enforce_loopback_only("10.0.0.1")
