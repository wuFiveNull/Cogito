# cogito/infrastructure/sandbox/secret_redactor.py
#
# DefaultSecretRedactor — strips secrets from tool results, logs, and events.
#
# Design rules (see tool-system-spec §21):
#   - Pattern-based: detect API keys, tokens, credentials by format.
#   - Key-based: recursively walk dicts and redact values at known keys.
#   - Redaction happens BEFORE model injection, logging, and persistence.
#   - Redacted values become ``[REDACTED]`` with a type hint.
#   - Never modify the original data — always return a copy.

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)


# Default secret patterns (regex)
_DEFAULT_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI / Anthropic / generic sk- keys
    re.compile(r'\b(sk-[a-zA-Z0-9]{20,})\b'),
    # GitHub personal access tokens
    re.compile(r'\b(ghp_[a-zA-Z0-9]{36,})\b'),
    re.compile(r'\b(gho_[a-zA-Z0-9]{36,})\b'),
    re.compile(r'\b(ghu_[a-zA-Z0-9]{36,})\b'),
    # GitLab tokens
    re.compile(r'\b(glpat-[a-zA-Z0-9\-_]{20,})\b'),
    # AWS access key
    re.compile(r'\b(AKIA[0-9A-Z]{16})\b'),
    # Bearer / Basic auth headers inline
    re.compile(r'(?i)(Bearer\s+)[a-zA-Z0-9\-_\.]{20,}'),
    re.compile(r'(?i)(Basic\s+)[a-zA-Z0-9=+/]{20,}'),
    # Authorization header value
    re.compile(r'(?i)(Authorization:\s*)(Bearer|Basic)\s+\S+'),
    # Slack tokens
    re.compile(r'\b(xox[baprs]-[a-zA-Z0-9\-]{20,})\b'),
    # Google OAuth / service account
    re.compile(r'\b(ya29\.[a-zA-Z0-9\-_]{30,})\b'),
    # PEM private keys
    re.compile(r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----'),
    # Generic token patterns (hex or base64, 32+ chars)
    re.compile(r'\b([a-f0-9]{32,})\b'),
    re.compile(r'\b([a-zA-Z0-9+/=]{40,})\b'),
)

# Keys whose values should always be redacted (recursive dict walk)
_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "api_key", "api_secret", "api_key_id",
    "access_token", "refresh_token", "id_token",
    "auth_token", "secret_key", "secret",
    "password", "passwd", "pass",
    "authorization", "authorization_header",
    "cookie", "session_key",
    "credential", "credentials",
    "private_key", "public_key",
    "token", "bearer_token",
    "client_secret", "app_secret",
    "ssh_key", "ssh_private_key",
    "jwt", "jwt_token",
    "aws_secret_access_key",
    "azure_connection_string",
})

# Sensitive key suffixes — any key ending with these is redacted
_SENSITIVE_SUFFIXES: tuple[str, ...] = (
    "_secret", "_token", "_key", "_password",
    "_credential", "_auth",
)

# Keys that contain sensitive environment info
_ENV_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "PATH", "HOME", "USERPROFILE", "APPDATA",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
})


class DefaultSecretRedactor:
    """Redacts secrets from dictionaries, strings, and JSON.

    Usage::
        redactor = DefaultSecretRedactor()
        safe = redactor.redact_dict(raw_tool_result)
        safe_text = redactor.redact_text(raw_stdout)
    """

    def __init__(
        self,
        *,
        additional_patterns: list[re.Pattern[str]] | None = None,
        additional_keys: frozenset[str] | None = None,
        redact_format: str = "[REDACTED_{type}]",
    ) -> None:
        self._patterns = _DEFAULT_SECRET_PATTERNS
        if additional_patterns:
            self._patterns = self._patterns + tuple(additional_patterns)
        self._sensitive_keys = _SENSITIVE_KEYS | (additional_keys or frozenset())
        self._sensitive_suffixes = _SENSITIVE_SUFFIXES
        self._redact_format = redact_format

    # ── Public API ──────────────────────────────────────────────────────

    def redact_text(self, text: str) -> str:
        """Redact secret patterns from a text string."""
        if not text:
            return text

        result = text
        for pattern in self._patterns:
            result = pattern.sub(self._replacement("SECRET"), result)
        return result

    def redact_dict(
        self,
        data: Mapping[str, object],
        *,
        depth: int = 0,
        max_depth: int = 15,
    ) -> dict[str, object]:
        """Recursively redact secrets from a dictionary."""
        if depth > max_depth:
            return dict(data)

        result: dict[str, object] = {}
        for key, value in data.items():
            # Key-based redaction (exact match + suffix match)
            key_lower = key.lower().replace("-", "_")
            if key_lower in self._sensitive_keys or self._matches_sensitive_suffix(key_lower):
                result[key] = self._replacement("KEY")
                continue

            # Recursive walk
            if isinstance(value, dict):
                result[key] = self.redact_dict(value, depth=depth + 1)
            elif isinstance(value, list):
                result[key] = self._redact_list(value, depth=depth)
            elif isinstance(value, str):
                result[key] = self.redact_text(value)
            else:
                result[key] = value

        return result

    def redact_json(self, json_str: str) -> str:
        """Redact secrets from a JSON string (parse → redact → serialize)."""
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            return self.redact_text(json_str)

        if isinstance(data, dict):
            return json.dumps(self.redact_dict(data), ensure_ascii=False)
        elif isinstance(data, list):
            return json.dumps(self._redact_list(data), ensure_ascii=False)
        return self.redact_text(json_str)

    # ── Internal ────────────────────────────────────────────────────────

    def _redact_list(
        self,
        items: list[object],
        depth: int = 0,
    ) -> list[object]:
        """Recursively redact items in a list."""
        result: list[object] = []
        for item in items:
            if isinstance(item, dict):
                result.append(self.redact_dict(item, depth=depth + 1))
            elif isinstance(item, list):
                result.append(self._redact_list(item, depth=depth))
            elif isinstance(item, str):
                result.append(self.redact_text(item))
            else:
                result.append(item)
        return result

    def _replacement(self, secret_type: str) -> str:
        return self._redact_format.format(type=secret_type)

    def _matches_sensitive_suffix(self, key: str) -> bool:
        """Check if a key ends with any sensitive suffix."""
        for suffix in self._sensitive_suffixes:
            if key.endswith(suffix):
                # Don't match if it's a common non-secret word
                if key in ("this_key", "the_key", "my_key"):
                    continue
                return True
        return False
