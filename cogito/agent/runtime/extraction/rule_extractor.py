# cogito/agent/runtime/extraction/rule_extractor.py
#
# DeterministicRuleExtractor — high-precision rule-based extraction.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.4):
#   - Rules are regex-based, matching explicit intent markers.
#   - Every match produces a RawPreference or RawMemory with initial
#     confidence, operation, and evidence text.
#   - All rule output still goes through the full validation+filtering
#     pipeline so that sensitive-data and conflict rules apply equally.
#   - ZH patterns are primary; EN patterns are secondary.

from __future__ import annotations

import re

from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawMemory,
    RawPreference,
)


# ── Preference patterns ─────────────────────────────────────────────────

# "Remember my <key> is <value>" (ZH / EN)
_PATTERN_REMEMBER = re.compile(
    r"(?:记住|记得|remember(?: that)?\s+)\s*(?:我[的])?\s*(?P<key>.+?)\s*(?:是|为|叫做|叫|is|:)\s*(?P<value>.+)",
    re.IGNORECASE,
)

# "From now on, please use <value>" (ZH / EN)
_PATTERN_FUTURE_USE = re.compile(
    r"(?:以后|今后|从今以后|from now on|in the future|going forward)"
    r"[，,、]?\s*(?:请|please|都|always)?\s*"
    r"(?:用|使用|回复|回答|speak|reply|respond|use)\s*"
    r"(?:中文|英文|日语|English|Chinese|Japanese|Python|Go|Rust|"
    r"简洁|详细|正式|正式风格)\s*",
    re.IGNORECASE,
)

# "Never use <format> again" / "不要再" (ZH / EN)
_PATTERN_AVOID = re.compile(
    r"(?:不要|别再|不要再用?|不再需要|禁止|never (?:use|show|do)|stop (?:using|showing|doing))\s*"
    r"(?P<format>.+?)(?:了|格式|形式|format|again)?\s*$",
    re.IGNORECASE,
)

# "Forget / delete my <key>" (ZH / EN)
_PATTERN_FORGET = re.compile(
    r"(?:忘掉|忘记|删除|清除|forget|delete|remove|clear)\s*(?:我[的])?\s*(?P<key>.+)",
    re.IGNORECASE,
)

# "I no longer like <key>" / "我不再喜欢" (ZH / EN)
_PATTERN_NO_LONGER = re.compile(
    r"(?:我不再|我不要|我不喜欢|我讨厌|我改[名变]|I (?:no longer |don'?t |do not ))"
    r"(?:喜欢|需要|想要|want|need|like|prefer)\s*(?P<key>.+)",
    re.IGNORECASE,
)


# ── Memory patterns ─────────────────────────────────────────────────────

# "Remember that <content>" (ZH / EN)
_PATTERN_REMEMBER_MEMORY = re.compile(
    r"(?:记住|记得|keep in mind|remember that|note that)\s*(?P<content>.+)",
    re.IGNORECASE,
)


class DeterministicRuleExtractor:
    """Extract candidates using deterministic pattern matching.

    This runs BEFORE the model-based extractor.  Its output feeds into
    the same validation, normalisation and deduplication pipeline.

    Thread-safety: stateless — safe to share across turns.
    """

    def extract(
        self,
        extraction_input: KnowledgeExtractionInput,
    ) -> RawKnowledgeExtraction:
        """Run all rules against the extraction input.

        Args:
            extraction_input: Prepared input (trimmed user + assistant text).

        Returns:
            RawKnowledgeExtraction with any rule-matched candidates.
        """
        user_text = extraction_input.user_text

        preferences: list[RawPreference] = []
        memories: list[RawMemory] = []
        summary: None = None  # rules don't produce summaries

        # ── Preference rules ─────────────────────────────────────────

        # Remember <key> is <value> → INSERT preference
        for m in _PATTERN_REMEMBER.finditer(user_text):
            key = m.group("key").strip()
            value = m.group("value").strip()
            if key and value:
                preferences.append(RawPreference(
                    key=key,
                    value=value,
                    operation="insert",
                    confidence=0.95,
                    content=f"{key}: {value}",
                    evidence_text=user_text[:200],
                    source_id=extraction_input.turn_id,
                ))

        # "以后用<lang>" → INSERT response.language
        for m in _PATTERN_FUTURE_USE.finditer(user_text):
            matched_text = m.group(0)
            lang = self._detect_language_preference(matched_text)
            if lang:
                preferences.append(RawPreference(
                    key="response.language",
                    value=lang,
                    operation="insert",
                    confidence=0.97,
                    content=f"response.language: {lang}",
                    evidence_text=user_text[:200],
                    source_id=extraction_input.turn_id,
                ))
            else:
                # Generic "use something" — capture the full intent
                preferences.append(RawPreference(
                    key="response.format",
                    value="concise",
                    operation="insert",
                    confidence=0.90,
                    content="response.format: concise",
                    evidence_text=user_text[:200],
                    source_id=extraction_input.turn_id,
                ))

        # "不要再<format>" → DELETE preference
        for m in _PATTERN_AVOID.finditer(user_text):
            fmt = m.group("format").strip()
            if fmt:
                preferences.append(RawPreference(
                    key=self._guess_key_from_text(fmt),
                    value=None,
                    operation="delete",
                    confidence=0.97,
                    content=f"delete: {fmt}",
                    evidence_text=user_text[:200],
                    source_id=extraction_input.turn_id,
                ))

        # "忘掉<key>" → DELETE preference
        for m in _PATTERN_FORGET.finditer(user_text):
            key = m.group("key").strip()
            if key:
                preferences.append(RawPreference(
                    key=self._guess_key_from_text(key),
                    value=None,
                    operation="delete",
                    confidence=0.97,
                    content=f"delete: {key}",
                    evidence_text=user_text[:200],
                    source_id=extraction_input.turn_id,
                ))

        # "我不再喜欢<key>" → DELETE preference
        for m in _PATTERN_NO_LONGER.finditer(user_text):
            key = m.group("key").strip()
            if key:
                preferences.append(RawPreference(
                    key=self._guess_key_from_text(key),
                    value=None,
                    operation="delete",
                    confidence=0.90,
                    content=f"delete: {key}",
                    evidence_text=user_text[:200],
                    source_id=extraction_input.turn_id,
                ))

        # ── Memory rules ────────────────────────────────────────────

        for m in _PATTERN_REMEMBER_MEMORY.finditer(user_text):
            content = m.group("content").strip()
            if content and len(content) > 10:
                memories.append(RawMemory(
                    content=content,
                    memory_key=self._guess_memory_key(content),
                    memory_type="fact",
                    operation="insert",
                    confidence=0.85,
                    importance=0.70,
                    evidence_text=user_text[:200],
                    source_id=extraction_input.turn_id,
                ))

        return RawKnowledgeExtraction(
            preferences=tuple(preferences),
            memories=tuple(memories),
        )

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _detect_language_preference(text: str) -> str | None:
        text_lower = text.lower()
        if "中文" in text or "chinese" in text_lower:
            return "zh-CN"
        if "英文" in text or "english" in text_lower:
            return "en-US"
        if "日语" in text or "japanese" in text_lower:
            return "ja-JP"
        return None

    @staticmethod
    def _guess_key_from_text(text: str) -> str:
        """Map common user text to a canonical preference key."""
        text_lower = text.strip().lower()

        # Language
        if any(t in text_lower for t in ("中文", "英文", "日语", "language", "english", "chinese")):
            return "response.language"

        # Format
        if any(t in text_lower for t in ("表格", "table", "格式", "format", "markdown", "bullet")):
            return "response.format.table"

        # Verbosity
        if any(t in text_lower for t in ("太长", "太短", "简洁", "详细", "长", "short", "long", "concise", "detailed")):
            return "response.verbosity"

        # Coding
        if any(t in text_lower for t in ("python", "go", "rust", "java", "代码", "coding", "编程")):
            return "coding.language"

        # Generic
        cleaned = text.replace(" ", "_").replace("的", "_").replace("了", "")
        return f"user.{cleaned}" if cleaned else "user.unknown"

    @staticmethod
    def _guess_memory_key(content: str) -> str:
        """Generate a memory key from content."""
        words = content.strip().split()[:5]
        stem = "_".join(words).lower()
        stem = "".join(c for c in stem if c.isalnum() or c in "_- ")
        stem = stem.replace(" ", "_")[:100]
        return f"rule_{stem}" if stem else "rule_remembered"
