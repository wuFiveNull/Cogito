"""Drift 结果 → ProactiveCandidate 投影服务 (R9 M6)。

校验来源（drift_run 必须 completed）+ Principal，生成幂等
ProactiveCandidate(origin=drift)。同一 DriftRun 最多生成一个用户可见
Candidate；可生成多个 internal result item。

dry_run 仅保存 preview，不创建真实 Candidate/Delivery —— 保留 Quiet Hours、
budget、cooldown、Endpoint 选择与 dry-run 控制。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from cogito.domain.drift import DriftCandidateDraft, DriftRunStatus
from cogito.store.proactive_repo import ProactiveCandidate

_LOGGER = logging.getLogger(__name__)


class DriftProjectionService:
    """将已完成的 Drift run 投影为 ProactiveCandidate。"""

    def __init__(self, conn: Any, dry_run: bool = False) -> None:
        self._conn = conn
        self._dry_run = dry_run

    def project(
        self, *, drift_run_id: str, draft: DriftCandidateDraft, principal_id: str = "owner"
    ) -> str | None:
        """从 draft 生成 ProactiveCandidate(origin=drift)。

        Returns candidate_id 或 None（dry_run / 重复投影 / run 未完成）。
        """
        # 校验 run 状态
        row = self._conn.execute(
            "SELECT status, principal_id FROM drift_runs WHERE drift_run_id=?",
            (drift_run_id,),
        ).fetchone()
        if row is None:
            _LOGGER.warning("drift_project: run %s not found", drift_run_id)
            return None
        if row["status"] != DriftRunStatus.completed.value:
            _LOGGER.warning(
                "drift_project: run %s not completed (status=%s)", drift_run_id, row["status"]
            )
            return None
        if row["principal_id"] != principal_id:
            _LOGGER.warning("drift_project: principal mismatch")
            return None

        # 同一 DriftRun 最多生成一个用户可见 Candidate
        existing = self._conn.execute(
            "SELECT 1 FROM proactive_candidates WHERE origin='drift' "
            "AND source_payload_ref=? LIMIT 1",
            (drift_run_id,),
        ).fetchone()
        if existing is not None:
            _LOGGER.debug("drift_project: run %s already projected", drift_run_id)
            return None

        if self._dry_run:
            _LOGGER.info(
                "[dry_run] drift_project would create candidate: run=%s topic=%s summary=%s",
                drift_run_id,
                draft.topic,
                draft.summary[:80],
            )
            return None

        # 幂等键
        idempotency = f"drift-projection:{drift_run_id}"
        dup = self._conn.execute(
            "SELECT candidate_id FROM proactive_candidates WHERE idempotency_key=?",
            (idempotency,),
        ).fetchone()
        if dup is not None:
            return dup["candidate_id"]

        now = int(time.time() * 1000)
        candidate_id = f"pc-drift-{uuid.uuid4().hex[:16]}"
        cand = ProactiveCandidate(
            candidate_id=candidate_id,
            principal_id=principal_id,
            stream_type="context",
            topic=draft.topic,
            summary=draft.summary,
            novelty=0.6,
            relevance=min(1.0, draft.relevance),
            urgency=min(1.0, draft.urgency),
            confidence=min(1.0, draft.confidence),
            recommended_action="evaluate",
            policy_version=1,
            idempotency_key=idempotency,
            source_event_ids=draft.evidence_refs,
            source_payload_ref=drift_run_id,
            origin="drift",
            created_at=now,
            expires_at=draft.expires_at if draft.expires_at else None,
            status="evaluating",
        )
        from cogito.store.proactive_repo import ProactiveCandidateRepository

        ProactiveCandidateRepository(self._conn).insert(cand)
        self._conn.commit()
        _LOGGER.info("drift_project: created candidate %s for run %s", candidate_id, drift_run_id)
        return candidate_id

    def preview(self, *, drift_run_id: str, draft: DriftCandidateDraft) -> dict[str, Any]:
        """dry_run 模式：返回本应创建的 Candidate 预览。"""
        return {
            "origin": "drift",
            "drift_run_id": drift_run_id,
            "would_create": not self._dry_run,
            "draft": draft.to_dict(),
        }
