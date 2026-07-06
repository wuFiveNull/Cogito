# cogito/agent/runtime/extraction/sensitivity.py
#
# SensitivityPolicy — filters or reclassifies candidates that contain
# sensitive information.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.9):
#   - API keys, passwords, private keys, OTPs → never generate candidates.
#   - Precise address, high-sensitivity health info, financial accounts
#     → only TENTATIVE if user explicitly asks to remember.
#   - The policy runs BEFORE conflict resolution, so that deleted
#     sensitive data isn't re-inserted by a later step.

from __future__ import annotations

import logging
import re

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawMemory,
    RawPreference,
)

logger = logging.getLogger(__name__)


# ── Absolutely forbidden patterns (never persist) ───────────────────────

_FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    # API keys and tokens
    re.compile(r"(?:api[_-]?key|apikey|api[_-]?secret|access[_-]?token)['\"]?\s*[:=]\s*['\"]?\w{16,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI-style
    re.compile(r"ghp_[A-Za-z0-9_]{36,}"),  # GitHub PAT
    re.compile(r"ya29\.[A-Za-z0-9_-]{100,}"),  # Google OAuth
    # Passwords and OTPs
    re.compile(r"password['\"]?\s*[:=]\s*['\"]?\S{6,}", re.IGNORECASE),
    re.compile(r"otp['\"]?\s*[:=]\s*['\"]?\d{4,8}", re.IGNORECASE),
    re.compile(r"(?:验证码|密码|OTP|verification code)\s*[:：]\s*\w{4,10}"),
    # Private keys
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"ssh-rsa\s+A{4,}"),
]

# ── High-sensitivity patterns (TENTATIVE only with explicit consent) ───

_HIGH_SENSITIVITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:住址|地址|address|home address|postal code|zip code)", re.IGNORECASE),
    re.compile(r"(?:身份证|ID number|passport|social security|SSN)", re.IGNORECASE),
    re.compile(r"(?:信用卡|credit card|debit card|银行卡)", re.IGNORECASE),
    re.compile(r"(?:医疗|诊断|disease|diagnosis|medical condition|病历)", re.IGNORECASE),
]


class SensitivityPolicy:
    """Filter or reclassify candidates containing sensitive information."""

    def __init__(
        self,
        config: KnowledgeExtractionConfig,
    ) -> None:
        self._config = config

    def apply(
        self,
        *,
        candidates: RawKnowledgeExtraction,
        extraction_input: KnowledgeExtractionInput,
    ) -> RawKnowledgeExtraction:
        """Apply sensitivity filtering.

        Args:
            candidates: Validated candidates to filter.
            extraction_input: The extraction input (for consent check).

        Returns:
            RawKnowledgeExtraction with sensitive candidates removed
            or downgraded to TENTATIVE.
        """
        filtered_prefs: list[RawPreference] = []
        for pref in candidates.preferences:
            if not self._is_allowed(pref.key, pref.content):
                logger.debug("Preference %r rejected by sensitivity policy", pref.key)
                continue
            if self._is_high_sensitivity(pref.key, pref.content):
                if self._config.allow_sensitive_with_explicit_consent:
                    pref = RawPreference(
                        key=pref.key,
                        value=pref.value,
                        operation="tentative",
                        confidence=min(pref.confidence, 0.70),
                        content=pref.content,
                        evidence_text=pref.evidence_text,
                        source_id=pref.source_id,
                    )
                else:
                    continue
            filtered_prefs.append(pref)

        filtered_mems: list[RawMemory] = []
        for mem in candidates.memories:
            if self._is_forbidden(mem.content):
                logger.debug("Memory rejected: contains forbidden content")
                continue
            filtered_mems.append(mem)

        return RawKnowledgeExtraction(
            preferences=tuple(filtered_prefs),
            memories=tuple(filtered_mems),
            summary=candidates.summary,
        )

    def _is_allowed(self, key: str, content: str) -> bool:
        """Check if a candidate passes the absolute allow policy."""
        if self._is_forbidden(content):
            return False
        if self._is_forbidden(key):
            return False
        return True

    @staticmethod
    def _is_forbidden(text: str) -> bool:
        """Check text against absolutely forbidden patterns."""
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(text):
                return True
        return False

    @staticmethod
    def _is_high_sensitivity(key: str, content: str) -> bool:
        """Check if content matches high-sensitivity patterns."""
        combined = f"{key} {content}"
        for pattern in _HIGH_SENSITIVITY_PATTERNS:
            if pattern.search(combined):
                return True
        return False
