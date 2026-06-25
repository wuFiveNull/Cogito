# cogito/infrastructure/sandbox/guard_rule.py
#
# GuardRule — data model for YAML-defined tool-call security rules.
#
# Reference: QwenPaw RuleBasedToolGuardian

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True, slots=True)
class GuardRule:
    """A single security rule loaded from YAML.

    Rule fields:
      id:          Unique rule identifier (e.g. 'TOOL_CMD_DANGEROUS_RM')
      tools:       List of tool names this rule applies to (empty = all tools)
      params:      List of parameter names to scan (empty = all params)
      category:    Threat category (e.g. 'command_injection', 'code_execution')
      severity:    Severity level (LOW / MEDIUM / HIGH / CRITICAL)
      patterns:    Regex patterns to match against parameter values
      exclude_patterns:  Optional regex patterns that exclude a match
      description: Human-readable description of the threat
      remediation: Suggested action
    """
    id: str
    tools: tuple[str, ...] = ()
    params: tuple[str, ...] = ()
    category: str = ""
    severity: str = "MEDIUM"
    patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    description: str = ""
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class CompiledGuardRule:
    """A compiled rule ready for fast matching."""
    rule: GuardRule
    compiled_patterns: tuple[re.Pattern[str], ...] = ()
    compiled_exclude: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """A rule match result."""
    rule_id: str
    severity: str
    category: str
    description: str
    remediation: str
    matched_text: str | None = None
    matched_pattern: str | None = None
    tool_name: str | None = None
    param_name: str | None = None
