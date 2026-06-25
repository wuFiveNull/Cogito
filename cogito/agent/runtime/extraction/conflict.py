# cogito/agent/runtime/extraction/conflict.py
#
# CandidateConflictResolver — determines the final operation for each
# candidate based on current known state and the candidate itself.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.10):
#   - Runs against ctx-level state snapshots only (no Repository calls).
#   - Rules: no-existing + clear-value → INSERT
#            existing + same-value → IGNORE (reinforcement)
#            existing + different-value + explicit correction → UPDATE
#            existing + clear delete/negation → DELETE
#            weak inference → TENTATIVE

from __future__ import annotations

from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawMemory,
    RawPreference,
)


class CandidateConflictResolver:
    """Resolve extracted candidates against known current state.

    Thread-safety: stateless.
    """

    def resolve(
        self,
        *,
        candidates: RawKnowledgeExtraction,
        extraction_input: KnowledgeExtractionInput,
    ) -> RawKnowledgeExtraction:
        """Resolve operation for each candidate based on conflict rules.

        Args:
            candidates: Sensitivity-filtered candidates.
            extraction_input: Extraction input (for current_preferences).

        Returns:
            RawKnowledgeExtraction with operations corrected for
            conflicts.
        """
        # Build a set of existing canonical keys for fast lookup
        existing_keys: set[str] = set()
        if extraction_input.current_preferences:
            from cogito.agent.domain.preferences import PreferenceCandidate
            for p in extraction_input.current_preferences:
                if hasattr(p, "key") and p.key:
                    existing_keys.add(p.key)

        resolved_prefs = tuple(
            self._resolve_preference_op(pref, existing_keys)
            for pref in candidates.preferences
        )

        return RawKnowledgeExtraction(
            preferences=resolved_prefs,
            memories=candidates.memories,
            summary=candidates.summary,
        )

    def _resolve_preference_op(
        self,
        pref: RawPreference,
        existing_keys: set[str],
    ) -> RawPreference:
        """Resolve the operation for a single preference candidate."""
        op = pref.operation

        # Already explicitly tentative — keep as-is
        if op == "tentative":
            return pref

        # Delete operation on a non-existent key → IGNORE
        if op == "delete" and pref.key not in existing_keys:
            return RawPreference(
                key=pref.key,
                value=pref.value,
                operation="ignore",
                confidence=pref.confidence,
                content=pref.content,
                evidence_text=pref.evidence_text,
                source_id=pref.source_id,
            )

        # Insert on an already-existing key → UPDATE (not duplicate)
        if op == "insert" and pref.key in existing_keys:
            return RawPreference(
                key=pref.key,
                value=pref.value,
                operation="update",
                confidence=pref.confidence,
                content=pref.content,
                evidence_text=pref.evidence_text,
                source_id=pref.source_id,
            )

        # Low-confidence → downgrade to tentative
        if pref.confidence < 0.80 and op in ("insert", "update", "delete"):
            return RawPreference(
                key=pref.key,
                value=pref.value,
                operation="tentative",
                confidence=pref.confidence,
                content=pref.content,
                evidence_text=pref.evidence_text,
                source_id=pref.source_id,
            )

        return pref
