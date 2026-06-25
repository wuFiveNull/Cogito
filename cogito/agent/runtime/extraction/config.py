# cogito/agent/runtime/extraction/config.py
#
# Default configuration for the knowledge extraction phase.
#
# Provides a convenience factory so that callers can quickly get a
# sensible config without importing the domain type directly.

from __future__ import annotations

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig


def default_knowledge_extraction_config() -> KnowledgeExtractionConfig:
    """Return a default KnowledgeExtractionConfig with standard limits.

    The defaults are tuned for a general-purpose assistant with ZH/EN
    support.  Override fields directly on the returned object.
    """
    return KnowledgeExtractionConfig(
        enabled=True,
        max_user_text_chars=16_000,
        max_assistant_text_chars=24_000,
        max_preferences=12,
        max_memories=12,
        extraction_timeout_seconds=12.0,
        malformed_output_retries=1,
        minimum_candidate_confidence=0.55,
        tentative_confidence_threshold=0.80,
        explicit_auto_apply_threshold=0.90,
        minimum_memory_importance=0.60,
        summary_minimum_information_gain=0.15,
        allow_inferred_preferences=True,
        allow_sensitive_with_explicit_consent=True,
        emit_candidate_content_in_logs=False,
    )
