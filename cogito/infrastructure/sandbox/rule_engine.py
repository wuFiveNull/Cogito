# cogito/infrastructure/sandbox/rule_engine.py
#
# YAML Rule Engine — loads, compiles, and matches security rules from YAML.
#
# Reference: QwenPaw RuleBasedToolGuardian
#
# Usage:
#   engine = RuleEngine()
#   engine.load_builtin_rules()
#   match = engine.check("execute_shell_command", {"command": "rm -rf /"})

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Sequence

from cogito.infrastructure.sandbox.guard_rule import (
    CompiledGuardRule,
    GuardRule,
    RuleMatch,
)

logger = logging.getLogger(__name__)

# Default rules directory (shipped with the package).
_DEFAULT_RULES_DIR = Path(__file__).resolve().parent / "rules"

# Default rule files loaded when no explicit path is provided.
_DEFAULT_RULE_FILES: list[str] = [
    "dangerous_commands.yaml",
]


class RuleEngine:
    """YAML rule-based tool-call security engine.

    Loads rules from YAML files, compiles regex patterns, and provides
    fast matching against tool parameters.
    """

    def __init__(
        self,
        disabled_rule_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._rules: dict[str, CompiledGuardRule] = {}
        self._disabled_ids = disabled_rule_ids

    # ── Loading ─────────────────────────────────────────────────────

    def load_builtin_rules(self) -> int:
        """Load all built-in rule files from the package rules directory."""
        count = 0
        for filename in _DEFAULT_RULE_FILES:
            path = _DEFAULT_RULES_DIR / filename
            if path.exists():
                count += self.load_rules_from_yaml(path)
            else:
                logger.warning("Built-in rule file not found: %s", path)
        return count

    def load_rules_from_yaml(self, path: str | Path) -> int:
        """Load rules from a single YAML file.

        Returns the number of rules loaded.
        """
        try:
            import yaml
        except ImportError:
            logger.error("PyYAML is required to load rule files: pip install pyyaml")
            return 0

        path = Path(path)
        if not path.exists():
            logger.warning("Rule file not found: %s", path)
            return 0

        try:
            with open(path, encoding="utf-8") as f:
                raw_rules = yaml.safe_load(f)
        except Exception as exc:
            logger.error("Failed to load rule file %s: %s", path, exc)
            return 0

        if not isinstance(raw_rules, list):
            logger.warning("Rule file %s does not contain a list of rules", path)
            return 0

        count = 0
        for raw in raw_rules:
            try:
                self._add_rule(self._parse_rule(raw))
                count += 1
            except Exception as exc:
                logger.warning("Skipping invalid rule in %s: %s", path, exc)

        logger.info("Loaded %d rules from %s", count, path)
        return count

    def load_rules_from_dict(self, rules: list[dict[str, Any]]) -> int:
        """Load rules from a list of dicts (for programmatic use / tests)."""
        count = 0
        for raw in rules:
            try:
                self._add_rule(self._parse_rule(raw))
                count += 1
            except Exception as exc:
                logger.warning("Skipping invalid rule: %s", exc)
        return count

    def add_rule(self, rule: GuardRule) -> None:
        """Add a single rule programmatically."""
        self._add_rule(rule)

    def remove_rule(self, rule_id: str) -> None:
        """Remove a rule by ID."""
        self._rules.pop(rule_id, None)

    # ── Matching ───────────────────────────────────────────────────

    def check(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> list[RuleMatch]:
        """Check a tool call against all loaded rules.

        Returns a list of RuleMatch objects for each matching rule.
        """
        matches: list[RuleMatch] = []

        for compiled in self._rules.values():
            rule = compiled.rule

            # Check if rule is disabled
            if rule.id in self._disabled_ids:
                continue

            # Check tool filter
            if rule.tools and tool_name not in rule.tools:
                continue

            # Determine parameters to scan
            if rule.params:
                param_names = [p for p in rule.params if p in params]
            else:
                param_names = list(params.keys())

            for param_name in param_names:
                value = params[param_name]
                str_value = str(value)

                for pattern in compiled.compiled_patterns:
                    m = pattern.search(str_value)
                    if not m:
                        continue

                    # Check exclude patterns
                    if compiled.compiled_exclude:
                        for exclude_re in compiled.compiled_exclude:
                            if exclude_re.search(str_value):
                                break
                        else:
                            # No exclude matched — this is a real match
                            matches.append(RuleMatch(
                                rule_id=rule.id,
                                severity=rule.severity,
                                category=rule.category,
                                description=rule.description,
                                remediation=rule.remediation,
                                matched_text=m.group(0),
                                matched_pattern=pattern.pattern,
                                tool_name=tool_name,
                                param_name=param_name,
                            ))
                    else:
                        matches.append(RuleMatch(
                            rule_id=rule.id,
                            severity=rule.severity,
                            category=rule.category,
                            description=rule.description,
                            remediation=rule.remediation,
                            matched_text=m.group(0),
                            matched_pattern=pattern.pattern,
                            tool_name=tool_name,
                            param_name=param_name,
                        ))

        return matches

    def get_rule(self, rule_id: str) -> GuardRule | None:
        """Get a rule by ID."""
        compiled = self._rules.get(rule_id)
        return compiled.rule if compiled else None

    def list_rules(self) -> list[GuardRule]:
        """List all loaded rules."""
        return [c.rule for c in self._rules.values()]

    # ── Internal ───────────────────────────────────────────────────

    @staticmethod
    def _parse_rule(raw: dict[str, Any]) -> GuardRule:
        """Parse a raw dict into a GuardRule."""
        return GuardRule(
            id=str(raw.get("id", "")),
            tools=tuple(raw.get("tools", [])),
            params=tuple(raw.get("params", [])),
            category=str(raw.get("category", "")),
            severity=str(raw.get("severity", "MEDIUM")),
            patterns=tuple(raw.get("patterns", [])),
            exclude_patterns=tuple(raw.get("exclude_patterns", [])),
            description=str(raw.get("description", "")),
            remediation=str(raw.get("remediation", "")),
        )

    def _add_rule(self, rule: GuardRule) -> None:
        """Add a parsed rule and compile its patterns."""
        if not rule.id:
            logger.warning("Rule without ID will be skipped")
            return

        compiled_patterns = tuple(
            re.compile(p, re.IGNORECASE) for p in rule.patterns
        )
        compiled_exclude = tuple(
            re.compile(p) for p in rule.exclude_patterns
        )

        self._rules[rule.id] = CompiledGuardRule(
            rule=rule,
            compiled_patterns=compiled_patterns,
            compiled_exclude=compiled_exclude,
        )
