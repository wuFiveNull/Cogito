# cogito/agent/runtime/extraction/summary.py
#
# SummaryCandidateBuilder — builds a SummaryCandidate when the turn
# contains semantically meaningful change worth persisting.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.13):
#   - Focus on: user goals, completed actions, unresolved items,
#     important decisions, context that needs to carry forward.
#   - Summary is generated via rules, not a separate model call.
#   - Uses third-person or neutral factual expression.
#   - minimum_information_gain threshold prevents trivial updates.

from __future__ import annotations

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
)
from cogito.agent.domain.memory import SummaryCandidate


class SummaryCandidateBuilder:
    """Rule-based summary candidate builder.

    This implementation uses deterministic heuristics to decide if
    a summary update is warranted.  It does NOT call a model.
    """

    def __init__(self, config: KnowledgeExtractionConfig) -> None:
        self._config = config

    def build(
        self,
        *,
        extraction_input: KnowledgeExtractionInput,
        accepted_candidates: RawKnowledgeExtraction,
    ) -> SummaryCandidate | None:
        """Build a summary candidate if the turn warrants one.

        Args:
            extraction_input: The extraction input (for turn context).
            accepted_candidates: Final accepted candidates.

        Returns:
            A SummaryCandidate or None if there is insufficient
            information gain.
        """
        # Determine if this turn had meaningful interaction
        user_text = extraction_input.user_text.strip()
        assistant_text = extraction_input.assistant_text.strip()

        if self._is_trivial_turn(user_text, assistant_text):
            return None

        # Check if any candidates were actually produced
        has_preferences = bool(accepted_candidates.preferences)
        has_memories = bool(accepted_candidates.memories)

        if not has_preferences and not has_memories and self._is_low_gain(user_text, assistant_text):
            return None

        # Build a summary entry
        summary_parts: list[str] = []
        pref_count = len(accepted_candidates.preferences)
        mem_count = len(accepted_candidates.memories)

        if pref_count > 0:
            summary_parts.append(f"{pref_count} preference(s) updated")
        if mem_count > 0:
            summary_parts.append(f"{mem_count} memory(s) noted")

        content_parts: list[str] = []
        if pref_count > 0 or mem_count > 0:
            content_parts.append("New knowledge extracted from this turn:")
            if pref_count > 0:
                pref_keys = [p.key for p in accepted_candidates.preferences[:3]]
                content_parts.append(f"  preferences: {', '.join(pref_keys)}")

        content = "\n".join(content_parts) if content_parts else ""

        if not content:
            return None

        return SummaryCandidate(
            content=content,
            confidence=0.85,
            candidate_id="",
            expected_version=None,
            source_refs=(extraction_input.turn_id,),
        )

    @staticmethod
    def _is_trivial_turn(user_text: str, assistant_text: str) -> bool:
        """Detect turns that don't warrant a summary update."""
        trivial_patterns = (
            "好的", "好的", "收到", "明白", "谢谢", "ok", "yes", "no",
            "thanks", "thank you", "got it", "sure",
        )
        return user_text.lower().strip() in trivial_patterns and len(assistant_text) < 20

    @staticmethod
    def _is_low_gain(user_text: str, assistant_text: str) -> bool:
        """Detect turns with minimal information gain."""
        return len(user_text) < 10 and len(assistant_text) < 50
