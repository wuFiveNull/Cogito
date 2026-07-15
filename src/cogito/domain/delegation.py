"""Deterministic child-Agent roles and budget normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DelegationRole:
    name: str
    description: str
    system_instruction: str
    allowed_toolsets: frozenset[str] | None
    read_only: bool
    budget: dict[str, int | float]


_BASE_BUDGET: dict[str, int | float] = {
    "max_loop_iterations": 6,
    "max_model_calls": 10,
    "max_tool_calls": 20,
    "max_input_tokens": 16_000,
    "max_output_tokens": 4_096,
    "max_wall_time_s": 120,
    "max_cost": 0.0,
}


DELEGATION_ROLES: dict[str, DelegationRole] = {
    "general": DelegationRole(
        name="general",
        description="General bounded task execution.",
        system_instruction="Solve the assigned task directly and report evidence and caveats.",
        allowed_toolsets=None,
        read_only=False,
        budget=dict(_BASE_BUDGET),
    ),
    "researcher": DelegationRole(
        name="researcher",
        description="Gather and synthesize information without changing the workspace.",
        system_instruction=(
            "Research the assigned question. Distinguish observed evidence from inference, "
            "cite the source identifiers available in Tool results, and do not modify files."
        ),
        allowed_toolsets=frozenset({"core", "memory", "search", "web", "knowledge"}),
        read_only=True,
        budget={**_BASE_BUDGET, "max_tool_calls": 16},
    ),
    "coder": DelegationRole(
        name="coder",
        description="Implement a focused workspace code change.",
        system_instruction=(
            "Implement only the assigned change. Inspect relevant code first, keep edits scoped, "
            "run focused verification when available, and report changed files and test results."
        ),
        allowed_toolsets=frozenset({"core", "memory", "search", "file", "knowledge"}),
        read_only=False,
        budget={**_BASE_BUDGET, "max_loop_iterations": 8},
    ),
    "reviewer": DelegationRole(
        name="reviewer",
        description="Review an implementation and return actionable findings.",
        system_instruction=(
            "Review the assigned implementation. Prioritize correctness, security, regressions, "
            "and missing tests. Do not edit files; return findings with precise locations."
        ),
        allowed_toolsets=frozenset({"core", "memory", "search", "file", "knowledge"}),
        read_only=True,
        budget={**_BASE_BUDGET, "max_loop_iterations": 5, "max_tool_calls": 12},
    ),
    "planner": DelegationRole(
        name="planner",
        description="Produce a concrete implementation plan without changing the workspace.",
        system_instruction=(
            "Analyze the assigned goal and produce an implementation-ready plan with dependencies, "
            "risks, acceptance checks, and explicit assumptions. Do not edit files."
        ),
        allowed_toolsets=frozenset({"core", "memory", "search", "file", "knowledge"}),
        read_only=True,
        budget={**_BASE_BUDGET, "max_loop_iterations": 5, "max_tool_calls": 10},
    ),
}


_INTEGER_BUDGET_FIELDS = (
    "max_loop_iterations",
    "max_model_calls",
    "max_tool_calls",
    "max_input_tokens",
    "max_output_tokens",
    "max_wall_time_s",
)


def resolve_delegation_role(value: Any) -> DelegationRole:
    name = str(value or "general").strip().lower()
    try:
        return DELEGATION_ROLES[name]
    except KeyError as exc:
        raise ValueError(f"unsupported child Agent role: {name}") from exc


def allocate_child_budget(
    *,
    role: DelegationRole,
    requested: dict[str, Any] | None,
    parent_budget: dict[str, Any] | None,
    parent_usage: dict[str, Any] | None,
    child_count: int,
) -> dict[str, int | float]:
    """Return a positive per-child budget within role and parent remaining limits."""
    request = requested or {}
    parent = parent_budget or {}
    usage = parent_usage or {}
    result: dict[str, int | float] = {}
    divisors = max(1, child_count)
    consumed_by_field = {
        "max_loop_iterations": "loop_iterations",
        "max_model_calls": "model_calls",
        "max_tool_calls": "tool_calls",
        "max_input_tokens": "total_tokens",
        "max_output_tokens": "output_tokens",
        "max_wall_time_s": "wall_time_s",
    }
    for field in _INTEGER_BUDGET_FIELDS:
        role_limit = _positive_int(role.budget[field], field)
        requested_limit = _positive_int(request.get(field, role_limit), field)
        parent_limit = _positive_int(parent.get(field, role_limit * divisors), field)
        consumed = max(0, int(usage.get(consumed_by_field[field], 0) or 0))
        remaining = parent_limit - consumed
        if remaining < divisors:
            raise ValueError(f"parent Agent has insufficient remaining {field}")
        per_child_parent_limit = remaining // divisors
        result[field] = min(role_limit, requested_limit, per_child_parent_limit)
    role_cost = float(role.budget.get("max_cost", 0.0) or 0.0)
    requested_cost = float(request.get("max_cost", role_cost) or 0.0)
    parent_cost = float(parent.get("max_cost", 0.0) or 0.0)
    consumed_cost = max(0.0, float(usage.get("cost", 0.0) or 0.0))
    finite_costs = [value for value in (role_cost, requested_cost) if value > 0]
    if parent_cost > 0:
        remaining_cost = parent_cost - consumed_cost
        if remaining_cost <= 0:
            raise ValueError("parent Agent has insufficient remaining max_cost")
        finite_costs.append(remaining_cost / divisors)
    result["max_cost"] = min(finite_costs) if finite_costs else 0.0
    return result


def select_role_toolsets(
    role: DelegationRole,
    *,
    parent_toolsets: set[str],
    requested_toolsets: set[str],
) -> set[str]:
    available = set(parent_toolsets)
    if role.allowed_toolsets is not None:
        available &= set(role.allowed_toolsets)
    return available & requested_toolsets if requested_toolsets else available


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed
