# cogito/infrastructure/sandbox/file_path_guardian.py
#
# FilePathToolGuardian — cross-tool sensitive file path protection.
#
# Unified interception of sensitive file access across all file-tools and
# shell commands.  Extracts candidate paths from shell command strings
# using shlex.split + redirection detection.
#
# Reference: QwenPaw FilePathToolGuardian

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)


# ── Default sensitive paths ────────────────────────────────────────────

_DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    # Credential files
    "*/.env",
    "*/.env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    # Config files with secrets
    "*/config.json",
    "*/config.yaml",
    "*/config.yml",
    "*/configuration/*",
    # Git internals
    "*/.git/config",
    "*/.git-credentials",
    # SSH keys
    "~/.ssh/*",
    # Database files
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    # Token files
    "*token*",
    "*secret*",
    "*.cred",
    "*.credential",
)


# ── Path inspection helpers ────────────────────────────────────────────

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_UNC_RE = re.compile(r"^\\\\[^\\/?%*:|\"<>]+[\\/][^\\/?%*:|\"<>]+")
_MIME_PREFIXES = ("text/", "application/", "image/", "audio/", "video/")
_SHELL_REDIRECT_OPS = frozenset({">", ">>", "1>", "1>>", "2>", "2>>", "&>", "<", "<<", "<<<"})


def _looks_like_path(value: str) -> bool:
    """Heuristic: does *value* look like a file path?"""
    if not value or value.startswith("-") or not isinstance(value, str):
        return False
    lower = value.lower()
    if lower.startswith(("http://", "https://", "ftp://", "data:")):
        return False
    if lower.startswith(_MIME_PREFIXES):
        return False
    posix = value.startswith(("/", "./", "../", "~")) or "/" in value
    windows = bool(_WIN_DRIVE_RE.match(value) or _WIN_UNC_RE.match(value))
    return bool(posix or windows)


def _normalize_path(raw: str, workspace: str | None = None) -> str:
    """Normalize a path to canonical absolute form (lowercase on Windows)."""
    raw = raw.strip().strip("\"'")
    if not raw:
        return ""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        ws = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        p = ws / p
    try:
        resolved = str(p.resolve(strict=False))
    except (OSError, RuntimeError):
        resolved = str(p.absolute())
    if os.name == "nt":
        return resolved.lower()
    return resolved


def _match_glob(pattern: str, path: str) -> bool:
    """Simple glob matching supporting * and **."""
    import fnmatch
    if pattern.startswith("*"):
        # **/ or */ prefix matches any directory depth
        suffix = pattern.lstrip("*").lstrip("/").lstrip("\\")
        if "**" in pattern:
            # ** means match any depth
            return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(
                Path(path).name, suffix
            )
        # * at start = match any prefix
        return path.endswith(suffix) or (
            "/" in path and path[path.index("/") + 1 :].endswith(suffix)
        )
    return fnmatch.fnmatch(path, pattern)


def _extract_paths_from_shell_command(command: str) -> list[str]:
    """Extract candidate file paths from a shell command string.

    Parses tokens via shlex (POSIX-aware), detects redirections,
    and returns anything that looks like a file path.
    """
    use_posix = os.name != "nt"
    try:
        tokens = shlex.split(command, posix=use_posix)
    except ValueError:
        tokens = command.split()

    # De-quote tokens (shlex with posix=False keeps quotes)
    if not use_posix:
        tokens = [t.strip("'\"") for t in tokens]

    candidates: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i].strip()

        # Redirection operator followed by path: cat a > out.txt
        if token in _SHELL_REDIRECT_OPS:
            if i + 1 < len(tokens):
                nxt = tokens[i + 1].strip().strip("'\"")
                if _looks_like_path(nxt):
                    candidates.append(nxt)
            i += 1
            continue

        # Attached redirect: 2>err.log, >out.txt
        for op in _SHELL_REDIRECT_OPS:
            if token.startswith(op) and len(token) > len(op):
                possible = token[len(op):].strip().strip("'\"")
                if _looks_like_path(possible):
                    candidates.append(possible)
                break
        else:
            if _looks_like_path(token):
                candidates.append(token)

        i += 1

    return list(dict.fromkeys(candidates))  # stable de-dup


# ── Result type ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FilePathGuardResult:
    """Result of a file path guard check."""
    is_blocked: bool = False
    reason: str | None = None
    matched_path: str | None = None
    matched_pattern: str | None = None


# ── Guardian ───────────────────────────────────────────────────────────

class FilePathToolGuardian:
    """Cross-tool sensitive file path protection.

    Blocks tool calls that reference files matching configured sensitive
    path patterns. Works with:

    - Known file tools (read_file, write_file, edit_file, etc.)
    - Shell commands (extracts paths via shlex)
    - String parameters heuristically scanned for paths
    """

    def __init__(
        self,
        sensitive_patterns: Iterable[str] | None = None,
        workspace: str | None = None,
    ) -> None:
        self._patterns: list[str] = list(sensitive_patterns or _DEFAULT_SENSITIVE_PATTERNS)
        self._workspace = workspace

    # ── Known file tools ────────────────────────────────────────────

    _FILE_TOOL_PARAMS: dict[str, tuple[str, ...]] = {
        "read_file": ("file_path",),
        "write_file": ("file_path",),
        "edit_file": ("file_path",),
        "apply_patch": ("file_path",),
        "list_dir": ("path",),
        "glob_search": ("pattern",),
    }

    def check_tool_call(self, tool_name: str, params: dict) -> FilePathGuardResult:
        """Check a tool call against sensitive file patterns."""
        # Known file tools: check specific params
        if tool_name in self._FILE_TOOL_PARAMS:
            for param in self._FILE_TOOL_PARAMS[tool_name]:
                value = params.get(param)
                if isinstance(value, str) and _looks_like_path(value):
                    result = self._check_path(value)
                    if result.is_blocked:
                        return result

        # Shell command: extract paths from command
        if tool_name in ("shell", "execute_shell_command"):
            command = params.get("command", "")
            if isinstance(command, str) and command:
                for candidate in _extract_paths_from_shell_command(command):
                    result = self._check_path(candidate)
                    if result.is_blocked:
                        return result

        # Generic string param scan: check all string values
        if tool_name not in self._FILE_TOOL_PARAMS and tool_name != "shell":
            for key, value in params.items():
                if isinstance(value, str) and _looks_like_path(value) and len(value) > 5:
                    result = self._check_path(value)
                    if result.is_blocked:
                        return result

        return FilePathGuardResult()

    def add_pattern(self, pattern: str) -> None:
        """Add a sensitive file glob pattern."""
        if pattern not in self._patterns:
            self._patterns.append(pattern)

    # ── Internal ────────────────────────────────────────────────────

    def _check_path(self, raw_path: str) -> FilePathGuardResult:
        """Check a single candidate path against all sensitive patterns."""
        normalized = _normalize_path(raw_path, self._workspace)
        if not normalized:
            return FilePathGuardResult()

        for pattern in self._patterns:
            # Normalize the pattern too
            norm_pattern = pattern.replace("\\", "/")
            if _match_glob(norm_pattern, normalized):
                return FilePathGuardResult(
                    is_blocked=True,
                    reason=f"Path matches sensitive file pattern: {pattern}",
                    matched_path=raw_path,
                    matched_pattern=pattern,
                )

            # Also match against just the filename
            if "/" not in norm_pattern or norm_pattern.startswith("*"):
                if _match_glob(norm_pattern, Path(normalized).name):
                    return FilePathGuardResult(
                        is_blocked=True,
                        reason=f"Filename matches sensitive pattern: {pattern}",
                        matched_path=raw_path,
                        matched_pattern=pattern,
                    )

        return FilePathGuardResult()
