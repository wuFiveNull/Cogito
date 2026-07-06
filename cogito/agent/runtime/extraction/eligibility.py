# cogito/agent/runtime/extraction/eligibility.py
#
# ExtractionEligibilityEvaluator — determines whether the turn warrants
# an LLM-based structured extraction call.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.2):
#   - Pure confirmation words → skip.
#   - One-shot conversion requests → skip unless a delete intent is
#     explicitly expressed.
#   - Content that wholly matches the "no-store" policy → skip.

from __future__ import annotations

import re

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
from cogito.agent.domain.knowledge.extraction import KnowledgeExtractionInput


# Minimal affirmative / acknowledgment patterns (ZH + EN)
_SKIP_TEXT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(好的|好[的嘛]|收到|明白|知道了|可以|嗯|ok|okay|thanks|thank you|谢谢|谢谢您)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(yes|yep|sure|got it|understood|fine|great|perfect)\s*$", re.IGNORECASE),
]


class ExtractionEligibilityEvaluator:
    """Decide if an LLM-based structured extraction call is worthwhile."""

    def __init__(self, config: KnowledgeExtractionConfig) -> None:
        self._config = config

    def should_call_model(self, extraction_input: KnowledgeExtractionInput) -> bool:
        """Return True if the extraction phase should call the model port.

        Even when this returns False, deterministic rule extraction may
        still run (e.g. explicit "forget" patterns).
        """
        if not self._config.enabled:
            return False

        user_text = extraction_input.user_text.strip()
        assistant_text = extraction_input.assistant_text.strip()

        # Both empty — nothing to extract from
        if not user_text and not assistant_text:
            return False

        # User only sent a confirmation / acknowledgment
        if self._is_skip_pattern(user_text) and not self._has_meaningful_content(assistant_text):
            return False

        return True

    @staticmethod
    def _is_skip_pattern(text: str) -> bool:
        return any(p.match(text) for p in _SKIP_TEXT_PATTERNS)

    @staticmethod
    def _has_meaningful_content(text: str) -> bool:
        return len(text.strip()) > 3
