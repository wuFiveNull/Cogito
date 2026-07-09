"""ResourceBudget + LoopState budget tracking — Plan 02 M4."""
from __future__ import annotations

import pytest

from cogito.runtime.loop import (
    AgentLoop,
    LoopState,
    ResourceBudget,
)
from cogito.model.contracts import Usage


# ---------------------------------------------------------------------------
# 1. ResourceBudget is frozen, serializable, and complete
# ---------------------------------------------------------------------------


def test_budget_roundtrip() -> None:
    budget = ResourceBudget(
        max_loop_iterations=5, max_model_calls=10, max_tool_calls=20,
        max_input_tokens=16000, max_output_tokens=4096,
        max_wall_time_s=60, max_cost=1.0,
    )
    data = budget.to_dict()
    restored = ResourceBudget.from_dict(data)
    assert restored.max_loop_iterations == 5
    assert restored.max_cost == 1.0


def test_budget_frozen() -> None:
    budget = ResourceBudget()
    with pytest.raises(AttributeError):
        budget.max_loop_iterations = 99  # type: ignore[misc]


def test_budget_defaults() -> None:
    budget = ResourceBudget()
    assert budget.max_loop_iterations == 10
    assert budget.max_model_calls == 20
    assert budget.max_tool_calls == 50
    assert budget.max_cost == 0.0  # 0 = unlimited


# ---------------------------------------------------------------------------
# 2. LoopState tracks model calls + budget
# ---------------------------------------------------------------------------


def test_loop_state_tracks_model_calls() -> None:
    state = LoopState()
    assert state.model_call_count == 0
    state.model_call_count += 1
    assert state.model_call_count == 1


def test_loop_state_has_budget() -> None:
    budget = ResourceBudget(max_loop_iterations=3)
    state = LoopState(budget=budget)
    assert state.budget.max_loop_iterations == 3


def test_loop_state_output_repaired_flag() -> None:
    """结构化输出失败只允许预算内修复一次。"""
    state = LoopState()
    assert state.output_repaired is False
    state.output_repaired = True
    assert state.output_repaired is True


# ---------------------------------------------------------------------------
# 3. AgentLoop accepts ResourceBudget
# ---------------------------------------------------------------------------


def test_agent_loop_accepts_budget() -> None:
    budget = ResourceBudget(max_loop_iterations=7, max_model_calls=15)
    loop = AgentLoop(router=_stub_router(), budget=budget)
    # budget is used inside run(); here we verify construction succeeds
    assert loop is not None


def test_agent_loop_legacy_params_build_budget() -> None:
    """Legacy max_iterations/max_tool_calls still construct a budget."""
    loop = AgentLoop(router=_stub_router(), max_iterations=4, max_tool_calls=8)
    assert loop is not None


# ---------------------------------------------------------------------------
# 4. Stub router (no model calls)
# ---------------------------------------------------------------------------


def _stub_router() -> Any:
    from cogito.model.router import ModelRouter
    from cogito.model.stub_provider import StubModelProvider
    provider = StubModelProvider()
    return ModelRouter(
        providers={"main": provider},
        role_map={"main": "main"},
    )


from typing import Any  # noqa: E402
