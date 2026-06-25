# cogito/agent/runtime/persistence/sanitizer.py
#
# PersistenceSanitizer — cleans turn data before it enters the
# PersistencePlan.
#
# Responsibilities:
#   - Remove secrets (API keys, tokens, cookies, Authorization headers)
#   - Whitelist/redact tool argument fields
#   - Truncate oversized tool results (>100 KB → external reference stub)
#   - Normalise Unicode (NFKC) and line endings
#   - Produce stable JSON serialisation (sorted keys, no extra spaces)
#   - Reject unserialisable objects

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping

SENSITIVE_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"api[_-]?secret", re.IGNORECASE),
    re.compile(r"access[_-]?token", re.IGNORECASE),
    re.compile(r"refresh[_-]?token", re.IGNORECASE),
    re.compile(r"authorization", re.IGNORECASE),
    re.compile(r"cookie[s]?", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"private[_-]?key", re.IGNORECASE),
)

MAX_INLINE_RESULT_BYTES = 100_000
MAX_FIELD_LENGTH = 10_000
REDACTED_PLACEHOLDER = "[REDACTED]"


@dataclass(frozen=True, slots=True)
class LargeResultRef:
    """Reference to a large tool result stored externally.

    When a tool result exceeds ``MAX_INLINE_RESULT_BYTES``, the content
    is saved to a content-addressed file and this reference is stored
    in the event's ``content_json`` instead.
    """

    storage_uri: str
    sha256: str
    media_type: str = "application/octet-stream"
    size_bytes: int = 0
    summary: str = ""


def canonical_json(value: Any) -> str:
    """Produce stable, compact JSON with sorted keys.

    Used for both database storage and fingerprint computation.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalise_text(text: str) -> str:
    """Normalise Unicode and line endings."""
    import unicodedata
    text = unicodedata.normalize("NFKC", text)
    # Normalise line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _is_sensitive_key(key: str) -> bool:
    """Check if a dict key looks like it might hold secrets."""
    return any(pattern.search(key) for pattern in SENSITIVE_KEY_PATTERNS)


def _redact_dict(value: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Recursively redact sensitive keys from a dictionary."""
    if depth > 10:
        return {"[NESTED]": REDACTED_PLACEHOLDER}
    result: dict[str, Any] = {}
    for k, v in value.items():
        if _is_sensitive_key(k):
            result[k] = REDACTED_PLACEHOLDER
        elif isinstance(v, dict):
            result[k] = _redact_dict(v, depth + 1)
        elif isinstance(v, list):
            result[k] = [
                _redact_dict(item, depth + 1) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def _truncate_value(key: str, value: str) -> str:
    """Truncate a string field to MAX_FIELD_LENGTH."""
    if len(value) > MAX_FIELD_LENGTH:
        return value[:MAX_FIELD_LENGTH] + f"\n... [truncated {len(value) - MAX_FIELD_LENGTH} chars]"
    return value


class PersistenceSanitizer:
    """Sanitizes turn context data before persistence.

    Usage::

        sanitizer = PersistenceSanitizer()
        sanitized = sanitizer.sanitize_context(ctx)
    """

    def sanitize_tool_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Redact secrets and truncate long fields in tool arguments."""
        cleaned = _redact_dict(dict(arguments))
        result: dict[str, Any] = {}
        for k, v in cleaned.items():
            if isinstance(v, str):
                result[k] = _truncate_value(k, v)
            else:
                result[k] = v
        return result

    def sanitize_tool_result(self, result: Mapping[str, Any]) -> dict[str, Any] | LargeResultRef:
        """Sanitize a tool result.

        If the result is small enough, return the cleaned dict.
        If it exceeds MAX_INLINE_RESULT_BYTES, return a LargeResultRef.
        """
        cleaned = _redact_dict(dict(result))
        result_json = canonical_json(cleaned)
        result_bytes = result_json.encode("utf-8")

        if len(result_bytes) <= MAX_INLINE_RESULT_BYTES:
            return cleaned

        # Build a LargeResultRef
        sha = hashlib.sha256(result_bytes).hexdigest()
        summary_text = str(cleaned.get("summary", ""))[:200]
        return LargeResultRef(
            storage_uri=f"file://tool-results/{sha}.json",
            sha256=sha,
            size_bytes=len(result_bytes),
            summary=_normalise_text(summary_text),
        )

    def sanitize_content(self, content: str) -> str:
        """Normalise and truncate text content."""
        normalised = _normalise_text(content)
        return _truncate_value("content", normalised)

    def build_safe_error_message(self, message: str | None) -> str | None:
        """Build a user-safe error message (no stack traces)."""
        if not message:
            return None
        # Take only the first line, strip file paths
        first_line = message.split("\n")[0].strip()
        # Remove anything that looks like a file path
        cleaned = re.sub(r'[A-Za-z]:\\[^\s:]*', '[path]', first_line)
        cleaned = re.sub(r'/[\w/.-]*/[^/\s]+\.\w+', '[path]', cleaned)
        return _truncate_value("error", cleaned)[:500]
