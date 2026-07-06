# cogito/agent/ports/sanitizer.py
#
# ContextSanitizerPort — lightweight text cleaning before injection.
#
# The ContextAssemblyPhase receives text from multiple sources
# (user input, retrieved documents, stored summaries, …) that may
# contain control characters, formatting noise or excessive length.
#
# This port handles *syntactic* cleanup only (control chars, Unicode
# normalisation, length limits).  It is NOT a prompt-injection
# defence — that requires structural separation (role boundaries,
# external-data markers, runtime policy enforcement).

from __future__ import annotations

import unicodedata
from typing import Protocol


class ContextSanitizerPort(Protocol):
    """Cleans text before it enters the model context.

    Implementations must be pure (no I/O, no model calls) and
    deterministic.
    """

    def sanitize_user_text(self, text: str) -> str:
        """Clean user-provided text (input, history)."""
        ...

    def sanitize_external_context(self, text: str) -> str:
        """Clean externally-sourced text (retrieval, summary, files)."""
        ...


class DefaultContextSanitizer:
    """Default implementation with common sanitisation rules.

    - Strips null bytes and most C0 control characters.
    - Normalises line endings (\\r\\n → \\n).
    - Applies Unicode NFKC normalisation.
    - Caps individual blocks at *max_text_chars*.
    """

    def __init__(
        self,
        *,
        max_text_chars: int = 100_000,
        normalize_unicode: bool = True,
    ) -> None:
        self._max_text_chars = max_text_chars
        self._normalize_unicode = normalize_unicode

    def sanitize_user_text(self, text: str) -> str:
        return self._sanitize(text)

    def sanitize_external_context(self, text: str) -> str:
        return self._sanitize(text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sanitize(self, text: str) -> str:
        if not text:
            return ""

        # Unicode normalisation
        if self._normalize_unicode:
            text = unicodedata.normalize("NFKC", text)

        # Normalise line endings
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # Strip null bytes and C0 control characters (keep \\t, \\n)
        cleaned_chars: list[str] = []
        for ch in text:
            cp = ord(ch)
            if cp == 0:
                continue  # null byte
            if cp < 0x20 and cp not in (0x09, 0x0A):
                continue  # other C0 controls except tab / newline
            cleaned_chars.append(ch)

        result = "".join(cleaned_chars)

        # Hard length cap
        if len(result) > self._max_text_chars:
            result = result[: self._max_text_chars]
            result += "\n[内容因长度限制已截断]"

        return result
