"""drift.run 完成后的 DriftResult 持久化 + 规范 Event 发射。

在 Drift Handler 完成事务中由 _finish_drift 调用（与 DriftRun 投影同一事务提交）。
Consumer (DriftResultCommittedConsumer) 校验 completed/principal/config/manifest 后
调 DriftProjectionService 写 ProactiveCandidate(origin=drift)；dry_run 只保存 preview，
不写真实 Candidate (PLAN-17 R5 P0-06)。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from cogito.domain.drift import DriftCandidateDraft, DriftReasonCode
from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore

_LOGGER = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_candidate_draft(items: list[dict[str, Any]]) -> DriftCandidateDraft | None:
    """从 Skill 内部项合成候选草稿 (仅结构占位，不授权)。"""
    if not items:
        return None
    first = items[0]
    summary_parts = []
    evidence_refs = []
    for it in items:
        s = it.get("summary") or it.get("kind") or ""
        if s:
            summary_parts.append(str(s))
        ref = it.get("ref") or it.get("id") or ""
        if ref:
            evidence_refs.append(str(ref))
    summary = "; ".join(summary_parts)[:500]
    if not summary:
        summary = first.get("kind", "drift.result")
    return DriftCandidateDraft(
        topic=first.get("kind", "drift.result"),
        summary=summary,
        evidence_refs=tuple(evidence_refs) if evidence_refs else (),
        trust_label="system_generated",
        urgency=0.5,
        confidence=0.5,
        relevance=0.6,
    )


def emit_drift_result(
    conn: sqlite3.Connection,
    *,
    drift_run_id: str,
    task_attempt_id: str,
    result_ref: str,
    summary: str,
    items: list[dict[str, Any]] | None = None,
    reason_code: DriftReasonCode | str | None = None,
    manifest_can_emit_candidate: bool = False,
) -> str | None:
    """为 Drift Handler 完成持久化 DriftResult + 发射规范 Event。

    - result_kind:
        - 'candidate_emission': 当 can_emit_candidate 且 items 非空 (供 Consumer 投影)
        - 'internal_only': items 非空但不能/无需投影
        - 'skipped_no_value': no_value / unknown skill

    同一事务 commit 由调用方（_drift.run handler）负责；本函数只执行 INSERT/emit。

    返回 drift_result_id, 或在 items 为空且不需要发射时返回 None。
    """
    from cogito.store.drift_result_repo import DriftResult, DriftResultRepository

    items = items or []
    rc_value = (
        reason_code.value
        if hasattr(reason_code, "value")
        else (str(reason_code) if reason_code else "")
    )

    has_items = bool(items)
    if has_items and manifest_can_emit_candidate:
        result_kind = "candidate_emission"
    elif has_items:
        result_kind = "internal_only"
    else:
        result_kind = "skipped_no_value"

    draft = None
    if result_kind == "candidate_emission":
        draft = build_candidate_draft(items)

    now = _now_ms()
    result_id = f"dr-res-{_uuid4_hex()[:16]}"
    DriftResultRepository(conn).insert(
        DriftResult(
            drift_result_id=result_id,
            drift_run_id=drift_run_id,
            task_attempt_id=task_attempt_id,
            result_kind=result_kind,
            result_ref=result_ref,
            summary=summary[:500] if summary else "",
            items=items,
            candidate_draft=draft.to_dict() if draft else None,
            emitted=False,
            created_at=now,
        )
    )

    # Candidate emission path / internal_only 都记录事实，
    # Consumer 决定实际投影（遵循"dry-run 仅 preview"语义由 DriftProjectionService 决定）。
    EventStore(conn).append(
        Event(
            event_type="drift.result.committed",
            stream_type="drift_result",
            stream_id=result_id,
            producer="drift-runner",
            event_class=EventClass.DOMAIN,
            context=EventContext(
                trace_id=drift_run_id,
                correlation_id=drift_run_id,
                attempt_id=task_attempt_id,
            ),
            summary="Drift result committed",
            # The existing consumer uses this stable result reference to find the
            # completed DriftRun; item bodies remain in DriftResult/PayloadStore.
            payload_ref=drift_run_id,
            attributes={
            "drift_run_id": drift_run_id,
            "drift_result_id": result_id,
            "result_kind": result_kind,
            "task_attempt_id": task_attempt_id,
            "can_emit_candidate": manifest_can_emit_candidate,
            "reason_code": rc_value,
            "has_items": has_items,
            "item_kinds": [str(it.get("kind", "")) for it in items],
            },
            outcome="committed",
            idempotency_key=f"drift-result:{result_id}:committed",
        )
    )
    _LOGGER.info(
        "drift result emitted: run=%s result=%s kind=%s (items=%d)",
        drift_run_id,
        result_id,
        result_kind,
        len(items),
    )
    return result_id


def _uuid4_hex() -> str:
    import uuid as _uuid

    return _uuid.uuid4().hex
