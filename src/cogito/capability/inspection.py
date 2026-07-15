"""Pure read-only projections for Tool and MCP control planes."""

from __future__ import annotations

from collections import Counter
from typing import Any

from cogito.capability.models import ToolDef


def tool_record(tool: ToolDef, *, include_schema: bool = False) -> dict[str, Any]:
    available = not tool.disabled
    reason = ""
    if available and tool.check_fn is not None:
        try:
            available = bool(tool.check_fn())
        except Exception as exc:
            available = False
            reason = _safe_error(exc)
    if tool.disabled:
        reason = "disabled"
    record: dict[str, Any] = {
        "name": tool.name,
        "capability_id": tool.capability_id,
        "source": tool.namespace,
        "toolsets": list(tool.toolset),
        "permissions": list(tool.permissions),
        "risk": tool.risk_level,
        "approval_policy": tool.approval_policy,
        "side_effect_class": tool.side_effect_class,
        "result_trust_label": tool.result_trust_label,
        "deferred": tool.deferred,
        "available": available,
        "reason": reason,
        "contract_issues": tool_contract_issues(tool),
    }
    if include_schema:
        record.update(
            {
                "description": tool.description,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
                "resource_requirements": dict(tool.resource_requirements),
            }
        )
    return record


def tool_contract_issues(tool: ToolDef) -> list[str]:
    issues: list[str] = []
    if not isinstance(tool.input_schema, dict) or not tool.input_schema:
        issues.append("missing_input_schema")
    if tool.output_schema is None:
        issues.append("missing_output_schema")
    if tool.result_trust_label not in {
        "verified_local", "external_untrusted", "user_supplied", "unverified",
    }:
        issues.append("invalid_trust_label")
    if tool.side_effect_class not in {
        "none", "idempotent", "reconcilable", "non_retriable",
    }:
        issues.append("invalid_side_effect_class")
    if tool.side_effect_class == "reconcilable" and tool.reconcile_fn is None:
        issues.append("missing_reconcile_handler")
    if tool.risk_level == "high" and tool.approval_policy == "never":
        issues.append("high_risk_never_approves")
    return issues


def tool_inventory(tools: list[ToolDef]) -> dict[str, Any]:
    records = [tool_record(tool) for tool in sorted(tools, key=lambda item: item.capability_id)]
    risk = Counter(record["risk"] for record in records)
    side_effects = Counter(record["side_effect_class"] for record in records)
    trust = Counter(record["result_trust_label"] for record in records)
    issue_count = sum(len(record["contract_issues"]) for record in records)
    return {
        "items": records,
        "total": len(records),
        "available_count": sum(bool(record["available"]) for record in records),
        "contract_complete": issue_count == 0,
        "contract_issue_count": issue_count,
        "risk": dict(sorted(risk.items())),
        "side_effects": dict(sorted(side_effects.items())),
        "trust_labels": dict(sorted(trust.items())),
    }


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
