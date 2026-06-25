# cogito/agent/runtime/extraction/normalizer.py
#
# CandidateNormalizer — normalises raw candidate values into canonical form.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.7):
#   - Unicode NFC normalisation.
#   - Language/timezone canonicalisation.
#   - Controlled key mapping.
#   - Synonym folding.
#   - Produces canonical_key and canonical_value.
#   - Does NOT modify operation or confidence — those belong to
#     ConfidenceCalibrator and ConflictResolver.

from __future__ import annotations

import unicodedata
from typing import Any

from cogito.agent.domain.knowledge.extraction import (
    RawKnowledgeExtraction,
    RawMemory,
    RawPreference,
)


# ── Canonical key map ────────────────────────────────────────────────────
# Maps common user expressions (lowercase, stripped) to canonical keys.

_CANONICAL_KEY_MAP: dict[str, str] = {
    # Response preferences
    "中文": "response.language",
    "英文": "response.language",
    "语言": "response.language",
    "language": "response.language",
    "回复语言": "response.language",
    "回答语言": "response.language",
    # Verbosity
    "简洁": "response.verbosity",
    "详细": "response.verbosity",
    "长度": "response.verbosity",
    "verbosity": "response.verbosity",
    # Format
    "表格": "response.format.table",
    "格式": "response.format",
    "markdown": "response.format",
    # Coding
    "python": "coding.language",
    "编程语言": "coding.language",
    "代码": "coding.language",
    # Style
    "语气": "response.style",
    "风格": "response.style",
    "formality": "response.tone",
    # Time
    "时区": "timezone",
    "timezone": "timezone",
    # Naming
    "称呼": "identity.preferred_name",
    "名字": "identity.preferred_name",
    "姓名": "identity.preferred_name",
}

# ── Language value canonicalisation ──────────────────────────────────────

_LANGUAGE_MAP: dict[str, str] = {
    "简体中文": "zh-CN",
    "中文（简体）": "zh-CN",
    "zh-cn": "zh-CN",
    "zh": "zh-CN",
    "chinese": "zh-CN",
    "中文": "zh-CN",
    "繁体中文": "zh-TW",
    "zh-tw": "zh-TW",
    "english": "en-US",
    "en": "en-US",
    "英文": "en-US",
    "日语": "ja-JP",
    "japanese": "ja-JP",
    "ja": "ja-JP",
    "日本语": "ja-JP",
}

# ── Verbosity map ──────────────────────────────────────────────────────

_VERBOSITY_MAP: dict[str, str] = {
    "简洁": "concise",
    "短": "concise",
    "简短": "concise",
    "verbose": "detailed",
    "详细": "detailed",
    "长": "detailed",
}


class CandidateNormalizer:
    """Normalise raw candidate keys and values to canonical form.

    Thread-safety: stateless.
    """

    def normalize(self, raw: RawKnowledgeExtraction) -> RawKnowledgeExtraction:
        """Normalise all candidates in a raw extraction result.

        Args:
            raw: Parsed (but not yet normalised) extraction result.

        Returns:
            RawKnowledgeExtraction with normalised keys and values.
        """
        normalised_prefs = tuple(self._normalise_preference(p) for p in raw.preferences if p)
        normalised_mems = tuple(self._normalise_memory(m) for m in raw.memories if m)

        return RawKnowledgeExtraction(
            preferences=normalised_prefs,
            memories=normalised_mems,
            summary=raw.summary,
        )

    def _normalise_preference(self, pref: RawPreference) -> RawPreference:
        key = self._normalise_text(pref.key)
        canonical_key = self._canonical_key(key)

        value = pref.value
        if value:
            value = self._normalise_text(value)
            value = self._canonical_value(canonical_key, value)

        content = self._normalise_text(pref.content) if pref.content else pref.content

        return RawPreference(
            key=canonical_key,
            value=value,
            operation=pref.operation,
            confidence=pref.confidence,
            content=content or canonical_key,
            evidence_text=pref.evidence_text,
            source_id=pref.source_id,
        )

    def _normalise_memory(self, mem: RawMemory) -> RawMemory:
        content = self._normalise_text(mem.content)
        memory_key = self._normalise_text(mem.memory_key)

        return RawMemory(
            content=content,
            memory_key=memory_key,
            memory_type=mem.memory_type,
            operation=mem.operation,
            confidence=mem.confidence,
            importance=mem.importance,
            evidence_text=mem.evidence_text,
            source_id=mem.source_id,
        )

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _normalise_text(text: str) -> str:
        """Apply Unicode NFC and strip whitespace."""
        return unicodedata.normalize("NFC", text).strip()

    @staticmethod
    def _canonical_key(raw_key: str) -> str:
        """Map a raw key to its canonical form.

        Checks the known map first (case-insensitive), then falls back
        to a cleaned version of the raw key.
        """
        lower = raw_key.lower().strip()
        # Direct map lookup
        for pattern, canonical in _CANONICAL_KEY_MAP.items():
            if pattern in lower:
                return canonical
        # Fallback: clean the raw key
        cleaned = lower.replace(" ", "_").replace("的", "_").replace("了", "")
        cleaned = "".join(c for c in cleaned if c.isalnum() or c in "_-.")
        return f"custom.{cleaned}" if cleaned else "custom.unknown"

    @staticmethod
    def _canonical_value(key: str, raw_value: str) -> str:
        """Map a raw value to its canonical form based on the key."""
        lower = raw_value.lower().strip()

        if key == "response.language":
            return _LANGUAGE_MAP.get(lower, lower)

        if key == "response.verbosity":
            return _VERBOSITY_MAP.get(lower, lower)

        return raw_value
