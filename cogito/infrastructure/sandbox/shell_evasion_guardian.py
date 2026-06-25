# cogito/infrastructure/sandbox/shell_evasion_guardian.py
#
# ShellEvasionGuardian — quote-aware shell obfuscation and evasion detection.
#
# Detects techniques that attempt to bypass regex-only command validation:
#   1. Command substitution ($(), ``, <(), =(), etc.)
#   2. ANSI-C / locale quoting ($'...', $"...") flag obfuscation
#   3. Backslash-escaped whitespace (\\  bypassing token splitting)
#   4. Backslash-escaped operators (\\;, \\|, \\&, \\<, \\>)
#   5. Newlines / carriage returns hiding commands
#   6. Comment-quote desync (# inside unquoted comment desyncs quote tracker)
#   7. Quoted newline + comment-line stripping attacks
#
# Reference: QwenPaw ShellEvasionGuardian

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


# ── Result type ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ShellEvasionResult:
    """Result of a shell evasion check."""
    is_evasion: bool = False
    reason: str | None = None
    check_name: str | None = None
    matched: str | None = None


# ── Quote-state tracker ────────────────────────────────────────────────

class _QuoteState:
    """Tracks shell quoting context character-by-character."""

    __slots__ = ("in_single", "in_double", "escaped")

    def __init__(self) -> None:
        self.in_single = False
        self.in_double = False
        self.escaped = False

    @property
    def in_any_quote(self) -> bool:
        return self.in_single or self.in_double

    def feed(self, char: str) -> None:
        """Advance the state machine by one character."""
        if self.escaped:
            self.escaped = False
            return

        if char == "\\" and not self.in_single:
            self.escaped = True
            return

        if char == "'" and not self.in_double:
            self.in_single = not self.in_single
            return

        if char == '"' and not self.in_single:
            self.in_double = not self.in_double


# ── Patterns ───────────────────────────────────────────────────────────

# Command substitution patterns (checked outside single quotes)
_COMMAND_SUBSTITUTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    (re.compile(r"=\("), "Zsh process substitution =()"),
    (re.compile(r"\$\("), "$() command substitution"),
    (re.compile(r"\$\["), "$[] legacy arithmetic expansion"),
    (re.compile(r"~\["), "Zsh-style parameter expansion"),
    (re.compile(r"\(e:"), "Zsh-style glob qualifiers"),
    (re.compile(r"\(\+"), "Zsh glob qualifier with command execution"),
    (re.compile(r"\}\s*always\s*\{"), "Zsh always block (try/always construct)"),
]

# Shell operators whose preceding backslash indicates evasion.
_SHELL_OPERATORS = frozenset(";|&<>")

# ANSI-C quoting: $'...'
_ANSI_C_QUOTE_RE = re.compile(r"\$'[^']*'")
# Locale quoting: $"..."
_LOCALE_QUOTE_RE = re.compile(r'\$"[^"]*"')
# Empty special quotes before dash: $'' -x or $"" -x
_EMPTY_SPECIAL_QUOTE_DASH_RE = re.compile(r"\$['\"]{2}\s*-")
# Empty regular quotes before dash: '' -x or "" -x
_EMPTY_QUOTE_DASH_RE = re.compile(r"(?:^|\s)(?:''|\"\")+\s*-")

# Heredoc opener detection
_HEREDOC_OPENER_RE = re.compile(
    r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1",
)

# find ... -exec ... {} \; is normal shell syntax, not evasion.
_FIND_EXEC_TERMINATOR_RE = re.compile(
    r"-(?:exec|execdir)\b[\s\S]*\{\}\s*\\;$",
)


# ── Helpers ────────────────────────────────────────────────────────────

def _extract_outside_single_quotes(command: str) -> str:
    """Return *command* with only single-quoted content removed.

    Double-quoted content is kept because shell still expands command
    substitutions inside double quotes.
    """
    state = _QuoteState()
    parts: list[str] = []
    for ch in command:
        was_single = state.in_single
        state.feed(ch)
        if not was_single and not state.in_single:
            parts.append(ch)
    return "".join(parts)


def _looks_like_heredoc(command: str) -> bool:
    """Return True when command appears to include a complete heredoc."""
    lines = command.splitlines()
    if len(lines) < 2:
        return False
    for i, line in enumerate(lines):
        m = _HEREDOC_OPENER_RE.search(line)
        if not m:
            continue
        delim = m.group(2)
        for next_line in lines[i + 1 :]:
            if next_line.strip() == delim:
                return True
    return False


# ── Individual checks ──────────────────────────────────────────────────

def _check_command_substitution(command: str, unquoted: str) -> ShellEvasionResult:
    """Detect command substitution patterns and unescaped backticks."""
    # Backtick check: unescaped ` outside single quotes.
    state = _QuoteState()
    for i, ch in enumerate(command):
        if state.escaped:
            state.feed(ch)
            continue
        state.feed(ch)
        if ch == "`" and not state.in_single and not state.escaped:
            return ShellEvasionResult(
                is_evasion=True,
                reason=f"Command contains backtick (`) command substitution at position {i}",
                check_name="command_substitution",
                matched=command[max(0, i - 10) : min(len(command), i + 10)],
            )

    # Other patterns checked against unquoted content
    for pattern, label in _COMMAND_SUBSTITUTION_PATTERNS:
        m = pattern.search(unquoted)
        if m:
            return ShellEvasionResult(
                is_evasion=True,
                reason=f"Command contains {label}",
                check_name="command_substitution",
                matched=m.group(0),
            )
    return ShellEvasionResult()


def _check_obfuscated_flags(command: str) -> ShellEvasionResult:
    """Detect ANSI-C / locale quoting and empty-quote flag obfuscation.

    These quoting mechanisms can hide flag characters (e.g. $'\\x2d exec'
    hides -exec) and bypass regex-based flag detection.
    """
    if _ANSI_C_QUOTE_RE.search(command):
        return ShellEvasionResult(
            is_evasion=True,
            reason="Command contains ANSI-C quoting ($'...') which can hide characters",
            check_name="obfuscated_flags",
        )

    if _LOCALE_QUOTE_RE.search(command):
        return ShellEvasionResult(
            is_evasion=True,
            reason='Command contains locale quoting ($"...") which can hide characters',
            check_name="obfuscated_flags",
        )

    if _EMPTY_SPECIAL_QUOTE_DASH_RE.search(command):
        return ShellEvasionResult(
            is_evasion=True,
            reason="Command contains empty special quotes before dash (potential flag bypass)",
            check_name="obfuscated_flags",
        )

    if _EMPTY_QUOTE_DASH_RE.search(command):
        return ShellEvasionResult(
            is_evasion=True,
            reason="Command contains empty quotes before dash (potential flag bypass)",
            check_name="obfuscated_flags",
        )

    # Quoted flag content: whitespace + quote + dash-letter inside quote
    state = _QuoteState()
    for i, ch in enumerate(command):
        if state.escaped:
            state.feed(ch)
            continue

        if not state.in_any_quote and (i == 0 or command[i - 1] in (" ", "\t")):
            if ch in ("'", '"'):
                quote_char = ch
                j = i + 1
                inside: list[str] = []
                while j < len(command) and command[j] != quote_char:
                    inside.append(command[j])
                    j += 1
                content = "".join(inside)
                if re.match(r"^-+[a-zA-Z0-9$`]", content):
                    return ShellEvasionResult(
                        is_evasion=True,
                        reason="Command contains quoted flag name (potential obfuscation)",
                        check_name="obfuscated_flags",
                        matched=command[i : j + 1],
                    )

        state.feed(ch)

    return ShellEvasionResult()


def _check_backslash_escaped_whitespace(command: str) -> ShellEvasionResult:
    r"""Detect backslash-escaped space/tab outside quotes.

    ``echo\ test`` is a single token in bash, but parsers may decode
    the escape and produce two separate tokens.
    """
    state = _QuoteState()
    for i, ch in enumerate(command):
        if state.escaped:
            if not state.in_double and ch in (" ", "\t"):
                return ShellEvasionResult(
                    is_evasion=True,
                    reason="Backslash-escaped whitespace could alter command parsing",
                    check_name="backslash_escaped_whitespace",
                    matched=command[max(0, i - 1) : i + 1],
                )
            state.feed(ch)
            continue
        state.feed(ch)
    return ShellEvasionResult()


def _check_backslash_escaped_operators(command: str) -> ShellEvasionResult:
    r"""Detect \;, \|, \&, \<, \> outside quotes.

    splitCommand normalises \; to bare ; causing false split on
    re-parsing, enabling arbitrary file reads.
    """
    state = _QuoteState()
    for i, ch in enumerate(command):
        if state.escaped:
            if not state.in_double and ch in _SHELL_OPERATORS:
                # find ... -exec ... {} \; is normal shell syntax.
                if ch == ";":
                    prefix = command[: i + 1]
                    if _FIND_EXEC_TERMINATOR_RE.search(prefix):
                        state.feed(ch)
                        continue
                return ShellEvasionResult(
                    is_evasion=True,
                    reason=f"Backslash before shell operator (\\{ch}) can hide command structure",
                    check_name="backslash_escaped_operators",
                    matched=command[max(0, i - 1) : i + 1],
                )
            state.feed(ch)
            continue
        state.feed(ch)
    return ShellEvasionResult()


def _check_newlines(command: str) -> ShellEvasionResult:
    """Detect newlines and carriage returns that could separate hidden commands."""
    # Heredoc intentionally relies on multiline input
    if _looks_like_heredoc(command):
        return ShellEvasionResult()

    # Carriage return outside double quotes (misparsing concern)
    state = _QuoteState()
    for ch in command:
        if state.escaped:
            state.feed(ch)
            continue
        state.feed(ch)
        if ch == "\r" and not state.in_double:
            return ShellEvasionResult(
                is_evasion=True,
                reason="Command contains carriage return (\\r) — shell-quote and bash tokenize differently",
                check_name="newlines",
            )

    # Newline outside quotes followed by non-whitespace (hidden command)
    state = _QuoteState()
    for i, ch in enumerate(command):
        if state.escaped:
            state.feed(ch)
            continue
        state.feed(ch)
        if ch in ("\n", "\r") and not state.in_any_quote:
            rest = command[i + 1 :]
            if rest.lstrip():
                return ShellEvasionResult(
                    is_evasion=True,
                    reason="Command contains newlines that could separate multiple commands",
                    check_name="newlines",
                )

    return ShellEvasionResult()


def _check_comment_quote_desync(command: str) -> ShellEvasionResult:
    """Detect quote characters inside # comments.

    Everything after an unquoted # is a comment.  Quote characters in
    comments desync quote state tracking for subsequent lines.
    """
    if "#" not in command:
        return ShellEvasionResult()

    state = _QuoteState()
    for i, ch in enumerate(command):
        if state.escaped:
            state.feed(ch)
            continue
        state.feed(ch)

        if ch == "#" and not state.in_any_quote:
            line_end = command.find("\n", i)
            comment = command[i + 1 : line_end if line_end != -1 else None]
            if re.search(r"['\"]", comment):
                return ShellEvasionResult(
                    is_evasion=True,
                    reason="Command contains quote characters inside a # comment — can desync quote tracking",
                    check_name="comment_quote_desync",
                    matched=command[i : (line_end if line_end != -1 else i + 40)],
                )
            # Skip rest of comment line
            if line_end == -1:
                break

    return ShellEvasionResult()


def _check_quoted_newline(command: str) -> ShellEvasionResult:
    """Detect newlines inside quoted strings where the next line starts with #.

    Line-based processing drops #-prefixed lines without tracking quote
    state, hiding arguments from path validation.
    """
    if "\n" not in command or "#" not in command:
        return ShellEvasionResult()

    state = _QuoteState()
    for i, ch in enumerate(command):
        if state.escaped:
            state.feed(ch)
            continue
        state.feed(ch)

        if ch == "\n" and state.in_any_quote:
            line_start = i + 1
            next_nl = command.find("\n", line_start)
            line_end = next_nl if next_nl != -1 else len(command)
            next_line = command[line_start:line_end]
            if next_line.strip().startswith("#"):
                return ShellEvasionResult(
                    is_evasion=True,
                    reason="Command contains a quoted newline followed by a #-prefixed line — can hide arguments",
                    check_name="quoted_newline",
                    matched=command[
                        max(0, i - 10) : min(len(command), line_end + 10)
                    ],
                )

    return ShellEvasionResult()


# ── Check registry ─────────────────────────────────────────────────────

_ShellCheckFn = Callable[..., ShellEvasionResult]
_CHECKS: tuple[tuple[str, _ShellCheckFn], ...] = (
    ("command_substitution", _check_command_substitution),
    ("obfuscated_flags", _check_obfuscated_flags),
    ("backslash_escaped_whitespace", _check_backslash_escaped_whitespace),
    ("backslash_escaped_operators", _check_backslash_escaped_operators),
    ("newlines", _check_newlines),
    ("comment_quote_desync", _check_comment_quote_desync),
    ("quoted_newline", _check_quoted_newline),
)
_CHECK_NAMES: frozenset[str] = frozenset(name for name, _ in _CHECKS)


# ── Public API ─────────────────────────────────────────────────────────

class ShellEvasionGuardian:
    """Quote-aware shell evasion / obfuscation detection.

    Detects 7 categories of shell obfuscation/evasion techniques.
    Designed to run *after* CommandPolicy (allowlist + dangerous pattern)
    as a second layer of defense.

    All checks are enabled by default.  Pass ``disabled_checks`` to
    selectively disable checks that produce false positives in your
    workflow.
    """

    def __init__(self, disabled_checks: frozenset[str] = frozenset()) -> None:
        self._disabled_checks = disabled_checks & _CHECK_NAMES

    def check(self, command: str) -> ShellEvasionResult:
        """Run all enabled evasion checks against *command*.

        Returns the **first** detected evasion result, or
        ``ShellEvasionResult()`` if no evasion is found.
        """
        if not command.strip():
            return ShellEvasionResult()

        outside_single_quotes = _extract_outside_single_quotes(command)

        for check_name, check in _CHECKS:
            if check_name in self._disabled_checks:
                continue
            try:
                if check_name == "command_substitution":
                    result = check(command, outside_single_quotes)
                else:
                    result = check(command)
            except Exception as exc:
                logger.warning(
                    "ShellEvasionGuardian check %s failed: %s",
                    check_name, exc,
                )
                continue
            if result.is_evasion:
                return result

        return ShellEvasionResult()

    def check_all(self, command: str) -> list[ShellEvasionResult]:
        """Run all enabled checks and return **all** findings.

        Useful for audit / debugging.  For inline enforcement use
        ``check()`` which short-circuits on first hit.
        """
        if not command.strip():
            return []

        outside_single_quotes = _extract_outside_single_quotes(command)
        findings: list[ShellEvasionResult] = []

        for check_name, check in _CHECKS:
            if check_name in self._disabled_checks:
                continue
            try:
                if check_name == "command_substitution":
                    result = check(command, outside_single_quotes)
                else:
                    result = check(command)
            except Exception as exc:
                logger.warning("ShellEvasionGuardian check %s failed: %s", check_name, exc)
                continue
            if result.is_evasion:
                findings.append(result)

        return findings
