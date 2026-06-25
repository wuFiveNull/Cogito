# cogito/infrastructure/sandbox/command_policy.py
#
# CommandPolicy — shell command allowlisting and dangerous pattern detection.
#
# Design rules (see tool-system-spec §18):
#   - Uses AST/Token parsing where possible, not just regex.
#   - Combines: command allowlist + dangerous pattern deny + env var allowlist.
#   - Deny by default: only explicitly allowed commands pass.
#   - Dangerous patterns trigger DENY regardless of allowlist.

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Sequence

from cogito.infrastructure.sandbox.rule_engine import RuleEngine

logger = logging.getLogger(__name__)


class CommandPolicyResult(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class CommandPolicyConfig:
    """Configuration for shell command policy."""
    allowed_commands: frozenset[str] = frozenset({
        "bash", "sh", "zsh",
        "python", "python3", "node", "npm", "npx",
        "git", "curl", "wget", "tar", "gzip", "gunzip",
        "cat", "head", "tail", "less", "more",
        "ls", "find", "grep", "rg", "ack",
        "wc", "sort", "uniq", "cut", "tr",
        "echo", "printf", "env", "which",
        "mkdir", "cp", "mv", "rm", "chmod",
        "diff", "patch", "sed", "awk",
        "date", "cal", "whoami", "hostname",
        "pwd", "cd", "test", "true", "false",
        "pip", "pip3",
    })
    dangerous_patterns: tuple[str, ...] = (
        # Privilege escalation
        r"\bsudo\b", r"\bsu\b", r"\bchown\b", r"\bpasswd\b",
        # Disk destruction
        r"\b(dd|mkfs|fdisk|parted|format)\b",
        # Recursive deletion of root / critical dirs
        r"\brm\s+-rf\s+/\s*$", r"\brm\s+-rf\s+~\s*$",
        r"\brm\s+-rf\s+[/\\](boot|etc|var|usr|sys|proc|dev)\b",
        r"\brm\s+-rf\s+[*?]",
        # Shutdown / reboot
        r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b", r"\binit\b",
        # Reverse shell patterns
        r"\bbash\s+-i\s*[>&]", r"\bexec\s+\d+[<>]",
        r"(sh|bash|python)\s+-c\s+['\"].*https?://",
        r"(wget|curl)\s+.*\|\s*(bash|sh|python)",
        # Fork bomb
        r":\(\)\s*\{", r"\bfork\s*bomb\b",
        # Python reverse shell via socket/subprocess
        r"python\s+-c\s+['\"].*\b(socket|subprocess)\b.*\b(socket|subprocess)\b",
        r"python\s+-c\s+['\"].*import\s+(socket|subprocess)",
        # Netcat reverse shell
        r"\bnc\s+-[eE]\s+",
        # Kill agent / parent
        r"\bkill\s+-?9?\s+(0|-1|\$\$|\$PPID)",
        r"\bpkill\s+-?9?\s+(cogito|python)",
        # Clear logs / audit trails
        r"\brm\s+.*\.(log|audit)\b", r"\bjournalctl\s+--rotate\b",
        # Kernel / system manipulation
        r"\bmodprobe\b", r"\binsmod\b", r"\brmmod\b",
        r"\bcat\s+/dev/(mem|kmem|port)",
        # Cryptominers / suspicious downloads
        r"\b(chmod|chattr)\s+\+?[0-9]{3,4}\s+.*bin",
    )
    allowed_env_keys: frozenset[str] = frozenset({
        "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
        "PATH", "HOME", "USER", "TERM", "SHELL",
        "TMPDIR", "TEMP", "TMP",
    })


class CommandPolicy:
    """Shell command policy — allowlist + dangerous pattern deny + YAML rule engine.

    This is the first line of defense before OS-level sandboxing.
    """

    def __init__(self, config: CommandPolicyConfig | None = None, rule_engine: RuleEngine | None = None) -> None:
        self._config = config or CommandPolicyConfig()
        self._compiled_patterns = [
            re.compile(p, re.IGNORECASE) for p in self._config.dangerous_patterns
        ]
        self._rule_engine = rule_engine

    def check(self, command: str) -> CommandPolicyResult:
        """Check a shell command against policy.

        1. Extract base command.
        2. Check against allowlist.
        3. Check for dangerous patterns (hardcoded + YAML rules).
        """
        if not command.strip():
            return CommandPolicyResult.DENY

        # Extract the base command (first token, after pipes trimmed)
        base_cmd = self._extract_base_command(command)
        if not base_cmd:
            return CommandPolicyResult.DENY

        # Allowlist check
        if base_cmd not in self._config.allowed_commands:
            logger.warning("Command blocked by allowlist: %s", base_cmd)
            return CommandPolicyResult.DENY

        # Dangerous pattern check (hardcoded)
        for pattern in self._compiled_patterns:
            if pattern.search(command):
                logger.warning(
                    "Command blocked by dangerous pattern: %s (matched %s)",
                    command[:120], pattern.pattern[:80],
                )
                return CommandPolicyResult.DENY

        # YAML rule engine check (loaded from rules/ directory)
        if self._rule_engine is not None:
            matches = self._rule_engine.check("shell", {"command": command})
            if matches:
                # Highest severity determines action
                severities = {m.severity for m in matches}
                if "CRITICAL" in severities or "HIGH" in severities:
                    match = matches[0]
                    logger.warning(
                        "Command blocked by YAML rule: %s (%s)",
                        match.rule_id, match.description,
                    )
                    return CommandPolicyResult.DENY

        return CommandPolicyResult.ALLOW

    def validate_env(self, env: dict[str, str]) -> dict[str, str]:
        """Filter environment variables through the allowlist."""
        return {
            k: v for k, v in env.items()
            if k in self._config.allowed_env_keys
        }

    @staticmethod
    def _extract_base_command(command: str) -> str | None:
        """Extract the base command from a shell command string."""
        # Strip leading variable assignments (e.g., "X=1 Y=2 cmd")
        tokens = command.strip().split()
        if not tokens:
            return None

        base = tokens[0]
        # Handle '=' assignment prefix (e.g., "CC=gcc make")
        while "=" in base and not base.startswith("-"):
            if len(tokens) > 1:
                tokens = tokens[1:]
                base = tokens[0]
            else:
                return None

        # Strip path prefix
        base = base.split("/")[-1].split("\\")[-1]

        # Handle shebang-like patterns
        base = base.strip("'\"")

        return base if base else None
