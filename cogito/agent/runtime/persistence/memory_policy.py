# cogito/agent/runtime/persistence/memory_policy.py
#
# MemoryPersistencePolicy — decides how to persist each memory
# candidate (fact, rule, event) within the transaction.

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from cogito.agent.domain.memory import MemoryCandidate
from cogito.agent.domain.preferences import CandidateOperation
from cogito.infrastructure.sqlite.repositories.memories import SQLiteMemoryRepository
from cogito.agent.runtime.persistence.models import CandidateWriteOutcome, PersistedEvent
from cogito.database.ids import new_uuid


def _normalise_memory_key(raw: str) -> str:
    """Normalise a memory key.

    Applies Unicode NFKC, strips whitespace, limits length.
    """
    import unicodedata
    key = unicodedata.normalize("NFKC", raw).strip()
    if not key:
        raise ValueError(f"Empty or invalid memory key: {raw!r}")
    return key[:200]


class MemoryPersistencePolicy:
    """Handles memory candidate persistence within a UoW transaction.

    Supports all five operations: insert, update, delete, ignore, tentative.
    For insert/update, checks for existing active records and either
    reinforces (equivalent content) or supersedes (changed content).
    """

    async def apply(
        self,
        *,
        candidates: tuple[MemoryCandidate, ...],
        memories: SQLiteMemoryRepository,
        persisted_events: tuple[PersistedEvent, ...],
        commit_id: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> list[CandidateWriteOutcome]:
        """Apply all memory candidates.

        Returns a list of CandidateWriteOutcome, one per candidate.
        """
        if not candidates:
            return []

        outcomes: list[CandidateWriteOutcome] = []
        event_ids = {e.event_id for e in persisted_events}

        for candidate in candidates:
            outcome = await self._apply_one(
                candidate=candidate,
                memories=memories,
                commit_id=commit_id,
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                now=now,
                event_ids=event_ids,
            )
            outcomes.append(outcome)

        return outcomes

    async def _apply_one(
        self,
        *,
        candidate: MemoryCandidate,
        memories: SQLiteMemoryRepository,
        commit_id: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
        event_ids: set[str],
    ) -> CandidateWriteOutcome:
        """Apply a single memory candidate."""
        if not candidate.memory_key:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="memory",
                candidate_key="",
                status="rejected",
                record_id=None,
                reason_code="empty_key",
            )

        try:
            normalised_key = _normalise_memory_key(candidate.memory_key)
        except ValueError:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="memory",
                candidate_key=candidate.memory_key,
                status="rejected",
                record_id=None,
                reason_code="invalid_key",
            )

        memory_type = candidate.memory_type or "fact"
        operation = candidate.operation or "insert"

        # Validate operation
        if operation == CandidateOperation.IGNORE:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="memory",
                candidate_key=normalised_key,
                status="ignored",
                record_id=None,
                reason_code="explicit_ignore",
            )

        if operation == CandidateOperation.TENTATIVE:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="memory",
                candidate_key=normalised_key,
                status="tentative",
                record_id=None,
                reason_code="low_confidence_tentative",
            )

        # Validate time range
        if candidate.valid_from and candidate.valid_until:
            if candidate.valid_until <= candidate.valid_from:
                return CandidateWriteOutcome(
                    candidate_id=candidate.candidate_id,
                    candidate_type="memory",
                    candidate_key=normalised_key,
                    status="rejected",
                    record_id=None,
                    reason_code="invalid_time_range",
                )

        # Find existing active record
        existing = await memories.get_active_by_key(
            user_id=user_id,
            memory_key=normalised_key,
        )

        source_ids = _resolve_source_event_ids(candidate, event_ids)

        if operation == CandidateOperation.DELETE:
            if existing:
                await memories.soft_delete(
                    memory_id=existing["id"],
                    updated_by_span_id=turn_id,
                )
                return CandidateWriteOutcome(
                    candidate_id=candidate.candidate_id,
                    candidate_type="memory",
                    candidate_key=normalised_key,
                    status="applied_delete",
                    record_id=existing["id"],
                    reason_code=None,
                )
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="memory",
                candidate_key=normalised_key,
                status="deduplicated",
                record_id=None,
                reason_code="already_deleted",
            )

        if operation in (CandidateOperation.UPDATE, CandidateOperation.INSERT):
            value_obj = candidate.value if candidate.value is not None else {}
            value_json = json.dumps(value_obj, ensure_ascii=False, sort_keys=True)

            if existing:
                if _is_equivalent(existing, candidate.content, value_json):
                    new_confidence = min(
                        1.0,
                        max(existing["confidence"], candidate.confidence) + 0.05,
                    )
                    new_importance = max(existing["importance"], candidate.importance)
                    await memories.update_reinforcement(
                        memory_id=existing["id"],
                        confidence=new_confidence,
                        importance=new_importance,
                        source_event_ids=tuple(source_ids),
                        updated_by_span_id=turn_id,
                    )
                    return CandidateWriteOutcome(
                        candidate_id=candidate.candidate_id,
                        candidate_type="memory",
                        candidate_key=normalised_key,
                        status="deduplicated",
                        record_id=existing["id"],
                        reason_code="reinforcement",
                    )
                else:
                    new_id = new_uuid()
                    await memories.mark_superseded(
                        memory_id=existing["id"],
                        valid_until=now,
                        updated_by_span_id=turn_id,
                    )
                    await memories.insert({
                        "id": new_id,
                        "user_id": user_id,
                        "memory_type": memory_type,
                        "memory_key": normalised_key,
                        "content": candidate.content,
                        "value_json": value_json,
                        "importance": candidate.importance,
                        "confidence": candidate.confidence,
                        "valid_from": candidate.valid_from,
                        "valid_until": candidate.valid_until,
                        "supersedes_id": existing["id"],
                        "source_event_ids_json": json.dumps(source_ids),
                        "created_by_span_id": turn_id,
                        "updated_by_span_id": turn_id,
                        "status": "active",
                    })
                    return CandidateWriteOutcome(
                        candidate_id=candidate.candidate_id,
                        candidate_type="memory",
                        candidate_key=normalised_key,
                        status="superseded",
                        record_id=new_id,
                        reason_code="content_changed",
                    )
            else:
                new_id = new_uuid()
                await memories.insert({
                    "id": new_id,
                    "user_id": user_id,
                    "memory_type": memory_type,
                    "memory_key": normalised_key,
                    "content": candidate.content,
                    "value_json": value_json,
                    "importance": candidate.importance,
                    "confidence": candidate.confidence,
                    "valid_from": candidate.valid_from,
                    "valid_until": candidate.valid_until,
                    "source_event_ids_json": json.dumps(source_ids),
                    "created_by_span_id": turn_id,
                    "updated_by_span_id": turn_id,
                    "status": "active",
                })
                return CandidateWriteOutcome(
                    candidate_id=candidate.candidate_id,
                    candidate_type="memory",
                    candidate_key=normalised_key,
                    status="applied_insert",
                    record_id=new_id,
                    reason_code=None,
                )

        return CandidateWriteOutcome(
            candidate_id=candidate.candidate_id,
            candidate_type="memory",
            candidate_key=normalised_key,
            status="rejected",
            record_id=None,
            reason_code=f"unknown_operation:{operation}",
        )


def _resolve_source_event_ids(
    candidate: MemoryCandidate,
    event_ids: set[str],
) -> list[str]:
    """Resolve source references to event IDs."""
    refs = list(candidate.source_refs) if candidate.source_refs else []
    return [ref for ref in refs if ref in event_ids] or refs


def _is_equivalent(existing: dict, new_content: str, new_value_json: str) -> bool:
    """Determine if a candidate is equivalent to an existing memory."""
    existing_content = existing.get("content", "")
    existing_value = existing.get("value_json", "{}")
    if isinstance(existing_value, str):
        pass
    else:
        existing_value = json.dumps(existing_value, ensure_ascii=False, sort_keys=True)

    if existing_content == new_content:
        return True
    if existing_value == new_value_json:
        return True
    return False
