from __future__ import annotations

import pytest

from cogito.domain.delegation import (
    allocate_child_budget,
    resolve_delegation_role,
    select_role_toolsets,
)


def test_role_toolsets_are_an_intersection() -> None:
    reviewer = resolve_delegation_role("reviewer")
    selected = select_role_toolsets(
        reviewer,
        parent_toolsets={"core", "file", "web", "subagent"},
        requested_toolsets={"file", "web", "subagent"},
    )
    assert selected == {"file"}
    assert reviewer.read_only is True


def test_child_budget_is_split_from_parent_remaining_budget() -> None:
    role = resolve_delegation_role("coder")
    budget = allocate_child_budget(
        role=role,
        requested={"max_loop_iterations": 7, "max_tool_calls": 15},
        parent_budget={
            "max_loop_iterations": 10,
            "max_model_calls": 20,
            "max_tool_calls": 50,
            "max_input_tokens": 32_000,
            "max_output_tokens": 8_192,
            "max_wall_time_s": 120,
            "max_cost": 0,
        },
        parent_usage={"loop_iterations": 1, "model_calls": 2, "tool_calls": 2},
        child_count=3,
    )
    assert budget["max_loop_iterations"] == 3
    assert budget["max_model_calls"] == 6
    assert budget["max_tool_calls"] == 15
    assert budget["max_input_tokens"] == 10_666


def test_child_budget_rejects_exhausted_parent() -> None:
    with pytest.raises(ValueError, match="insufficient remaining max_loop_iterations"):
        allocate_child_budget(
            role=resolve_delegation_role("general"),
            requested=None,
            parent_budget={"max_loop_iterations": 2},
            parent_usage={"loop_iterations": 1},
            child_count=2,
        )


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported child Agent role"):
        resolve_delegation_role("administrator")
