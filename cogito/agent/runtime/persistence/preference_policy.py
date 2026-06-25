# cogito/agent/runtime/persistence/preference_policy.py
#
# PreferencePersistencePolicy — decides how to persist each
# preference candidate within the transaction.
#
# Preference candidates are stored in the ``memories`` table with
# ``memory_type = 'preference'`` and a ``memory_key`` prefixed
# with ``preference.``.

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from cogito.agent.domain.preferences import PreferenceCandidate, CandidateOperation
from cogito.agent.ports.repositories_memory import MemoryRepositoryPort as MemRepo
from cogito.agent.runtime.persistence.models import CandidateWriteOutcome, PersistedEvent
from cogito.infrastructure.sqlite.repositories.memories import SQLiteMemoryRepository
from cogito.database.ids import new_uuid


def _normalise_key(raw: str) -> str:
    """Normalise a preference key.

    Rules:
      - Strip whitespace
      - Unicode NFKC
      - Lowercase English
      - Space and consecutive dots → single dot
      - Only allow [a-z0-9._-] and controlled Unicode
      - Max 200 characters
      - Prepend ``preference.`` prefix
    """
    import unicodedata
    key = unicodedata.normalize("NFKC", raw).strip().lower()
    key = key.replace(" ", ".").replace("..", ".")
    # Remove characters not in allowed set
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    key = "".join(c for c in key if c in allowed or ord(c) > 127)
    if not key:
        raise ValueError(f"Empty or invalid preference key: {raw!r}")
    key = key[:200]
    return f"preference.{key}"


class PreferencePersistencePolicy:
    """Handles preference candidate persistence within a UoW transaction.

    Each candidate produces a ``CandidateWriteOutcome`` that describes
    what was done (inserted, deduplicated, superseded, rejected, etc.).
    """

    async def apply(
        self,
        *,
        candidates: tuple[PreferenceCandidate, ...],
        memories: SQLiteMemoryRepository,
        persisted_events: tuple[PersistedEvent, ...],
        commit_id: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> list[CandidateWriteOutcome]:
        """Apply all preference candidates.

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
        candidate: PreferenceCandidate,
        memories: SQLiteMemoryRepository,
        commit_id: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
        event_ids: set[str],
    ) -> CandidateWriteOutcome:
        """Apply a single preference candidate."""
        try:
            normalised_key = _normalise_key(candidate.key)
        except ValueError:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="preference",
                candidate_key=candidate.key,
                status="rejected",
                record_id=None,
                reason_code="invalid_key",
            )

        operation = candidate.operation

        if operation == CandidateOperation.IGNORE:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="preference",
                candidate_key=normalised_key,
                status="ignored",
                record_id=None,
                reason_code="explicit_ignore",
            )

        if operation == CandidateOperation.TENTATIVE:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="preference",
                candidate_key=normalised_key,
                status="tentative",
                record_id=None,
                reason_code="low_confidence_tentative",
            )

        # Find existing active record with this key
        existing = await memories.get_active_by_key(
            user_id=user_id,
            memory_key=normalised_key,
        )

        if operation == CandidateOperation.DELETE:
            if existing:
                await memories.soft_delete(
                    memory_id=existing["id"],
                    updated_by_span_id=turn_id,
                )
                return CandidateWriteOutcome(
                    candidate_id=candidate.candidate_id,
                    candidate_type="preference",
                    candidate_key=normalised_key,
                    status="applied_delete",
                    record_id=existing["id"],
                    reason_code=None,
                )
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="preference",
                candidate_key=normalised_key,
                status="deduplicated",
                record_id=None,
                reason_code="already_deleted",
            )

        # INSERT or UPDATE
        source_ids = _resolve_source_event_ids(candidate, event_ids)
        value_json = json.dumps(
            candidate.value if candidate.value is not None else {},
            ensure_ascii=False,
        )

        if existing:
            # Check if content/value is equivalent
            if _is_equivalent(existing, candidate.content, value_json):
                # Reinforcement
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
                    candidate_type="preference",
                    candidate_key=normalised_key,
                    status="deduplicated",
                    record_id=existing["id"],
                    reason_code="reinforcement",
                )
            else:
                # Content changed — supersede old, insert new
                new_id = new_uuid()
                await memories.mark_superseded(
                    memory_id=existing["id"],
                    valid_until=now,
                    updated_by_span_id=turn_id,
                )
                await memories.insert({
                    "id": new_id,
                    "user_id": user_id,
                    "memory_type": "preference",
                    "memory_key": normalised_key,
                    "content": candidate.content,
                    "value_json": value_json,
                    "importance": candidate.importance,
                    "confidence": candidate.confidence,
                    "supersedes_id": existing["id"],
                    "source_event_ids_json": json.dumps(source_ids),
                    "created_by_span_id": turn_id,
                    "updated_by_span_id": turn_id,
                    "status": "active",
                })
                return CandidateWriteOutcome(
                    candidate_id=candidate.candidate_id,
                    candidate_type="preference",
                    candidate_key=normalised_key,
                    status="superseded",
                    record_id=new_id,
                    reason_code="content_changed",
                )
        else:
            # No existing — insert new
            new_id = new_uuid()
            await memories.insert({
                "id": new_id,
                "user_id": user_id,
                "memory_type": "preference",
                "memory_key": normalised_key,
                "content": candidate.content,
                "value_json": value_json,
                "importance": candidate.importance,
                "confidence": candidate.confidence,
                "source_event_ids_json": json.dumps(source_ids),
                "created_by_span_id": turn_id,
                "updated_by_span_id": turn_id,
                "status": "active",
            })
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="preference",
                candidate_key=normalised_key,
                status="applied_insert",
                record_id=new_id,
                reason_code=None,
            )


def _resolve_source_event_ids(
    candidate: PreferenceCandidate,
    event_ids: set[str],
) -> list[str]:
    """Resolve source references to event IDs.

    Uses the candidate's ``source_refs``.  Filters to only include
    references that match known event IDs.
    """
    candidates_list = list(candidate.source_refs) if candidate.source_refs else []
    # Also include backward-compat source_message_id
    if candidate.source_message_id and candidate.source_message_id not in candidates_list:
        candidates_list.append(candidate.source_message_id)
    return [ref for ref in candidates_list if ref in event_ids] or candidates_list


def _is_equivalent(existing: dict, new_content: str, new_value_json: str) -> bool:
    """Determine if a candidate is equivalent to an existing memory.

    Uses deterministic comparison (no LLM):
      - Same content → equivalent
      - Same value_json → equivalent
      - Otherwise → different (will supersede)
    """
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
