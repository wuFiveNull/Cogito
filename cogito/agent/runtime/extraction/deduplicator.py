# cogito/agent/runtime/extraction/deduplicator.py
#
# CandidateDeduplicator — removes duplicate candidates from the combined
# rule + model output.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.12):
#   - Same fingerprint → keep the higher-confidence one.
#   - Same key + same canonical value → merge evidence.
#   - Same key + different value → keep both for conflict resolution.
#   - Rule candidates take priority over model candidates on conflict.

from __future__ import annotations

from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawMemory,
    RawPreference,
)


class CandidateDeduplicator:
    """Deduplicate merged rule + model candidates.

    Thread-safety: stateless.
    """

    def deduplicate(
        self,
        candidates: RawKnowledgeExtraction,
        extraction_input: KnowledgeExtractionInput | None = None,
    ) -> RawKnowledgeExtraction:
        """Remove duplicate candidates.

        Args:
            candidates: The merged candidate set (rule + model).
            extraction_input: Optional, for source-based dedup.

        Returns:
            Deduplicated RawKnowledgeExtraction.
        """
        # Prefs: dedup by key + value
        seen_prefs: set[tuple[str, str]] = set()
        unique_prefs: list[RawPreference] = []
        for pref in candidates.preferences:
            key = (pref.key, pref.content)
            if key not in seen_prefs:
                seen_prefs.add(key)
                unique_prefs.append(pref)
            # Duplicate: keep the higher-confidence one
            else:
                for i, existing in enumerate(unique_prefs):
                    if existing.key == pref.key and existing.content == pref.content:
                        if pref.confidence > existing.confidence:
                            unique_prefs[i] = pref
                        break

        # Memories: dedup by memory_key + content
        seen_mems: set[tuple[str, str]] = set()
        unique_mems: list[RawMemory] = []
        for mem in candidates.memories:
            key = (mem.memory_key, mem.content)
            if key not in seen_mems:
                seen_mems.add(key)
                unique_mems.append(mem)

        return RawKnowledgeExtraction(
            preferences=tuple(unique_prefs),
            memories=tuple(unique_mems),
            summary=candidates.summary,
        )
