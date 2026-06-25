"""Tests for RepetitionGuard — tool call cycle and repetition detection."""

from __future__ import annotations

import pytest

from cogito.agent.domain.tools import (
    ToolCall,
    ToolCallPlan,
    ToolDefinition,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolKind,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
    PreparedToolCall,
    RejectedToolCall,
)
from cogito.agent.tools.repetition_guard import RepetitionGuard, RepetitionError


def _make_call(call_id: str, tool_name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments=args or {},
        arguments_json='{}',
        ordinal=0,
    )


def _make_prepared(call: ToolCall, fp: str = "fp_abc") -> PreparedToolCall:
    return PreparedToolCall(
        call=call,
        definition=_make_def(call.tool_name),
        idempotency_key=f"turn:{call.call_id}",
        arguments_fingerprint=fp,
    )


def _make_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Test",
        input_schema={"type": "object", "properties": {}},
        side_effect=ToolSideEffect.NONE,
        risk_level=ToolRiskLevel.LOW,
        timeout_seconds=30.0,
        idempotent=True,
        parallel_safe=True,
        kind=ToolKind.READ,
        risk=ToolRisk.READ_ONLY,
        source=ToolSource(type=ToolSourceType.BUILTIN, provider="test"),
    )


class TestRepetitionGuard:
    def test_no_repetition_allows_calls(self) -> None:
        """Calls with different fingerprints pass without error."""
        guard = RepetitionGuard(max_repeated_fingerprint=2)
        plan = ToolCallPlan(
            original_calls=(_make_call("c1", "read_file"), _make_call("c2", "list_dir")),
            executable_calls=(
                _make_prepared(_make_call("c1", "read_file", {"path": "/a"}), "fp_a"),
                _make_prepared(_make_call("c2", "list_dir", {"path": "/b"}), "fp_b"),
            ),
            rejected_calls=(),
        )

        guard.check_batch(plan)  # Should not raise

    def test_repeated_fingerprint_blocked(self) -> None:
        """Same fingerprint repeated beyond max is blocked."""
        guard = RepetitionGuard(max_repeated_fingerprint=2)

        # First call
        call1 = _make_call("c1", "read_file", {"path": "/a"})
        prep1 = _make_prepared(call1, "fp_same")
        plan1 = ToolCallPlan(
            original_calls=(call1,),
            executable_calls=(prep1,),
            rejected_calls=(),
        )
        guard.check_batch(plan1)
        guard.record_batch(plan1, [
            ToolExecutionResult(call_id="c1", tool_name="read_file", status=ToolExecutionStatus.SUCCEEDED, model_content="ok"),
        ])

        # Second call — same fingerprint — should succeed (at limit)
        call2 = _make_call("c2", "read_file", {"path": "/a"})
        prep2 = _make_prepared(call2, "fp_same")
        plan2 = ToolCallPlan(
            original_calls=(call2,),
            executable_calls=(prep2,),
            rejected_calls=(),
        )
        guard.check_batch(plan2)
        guard.record_batch(plan2, [
            ToolExecutionResult(call_id="c2", tool_name="read_file", status=ToolExecutionStatus.SUCCEEDED, model_content="ok"),
        ])

        # Third call — same fingerprint — should be blocked
        call3 = _make_call("c3", "read_file", {"path": "/a"})
        prep3 = _make_prepared(call3, "fp_same")
        plan3 = ToolCallPlan(
            original_calls=(call3,),
            executable_calls=(prep3,),
            rejected_calls=(),
        )

        with pytest.raises(RepetitionError) as exc:
            guard.check_batch(plan3)

        assert exc.value.code == "REPEATED_TOOL_CALL"

    def test_cycle_detection(self) -> None:
        """A-B-A-B cycle is detected."""
        guard = RepetitionGuard(max_repeated_fingerprint=10, cycle_window=8)

        calls = [
            ("c1", "search", "fp_search_1"),
            ("c2", "read", "fp_read_1"),
            ("c3", "search", "fp_search_2"),
            ("c4", "read", "fp_read_2"),
        ]

        for call_id, tool, fp in calls:
            tc = _make_call(call_id, tool)
            prep = _make_prepared(tc, fp)
            plan = ToolCallPlan(
                original_calls=(tc,),
                executable_calls=(prep,),
                rejected_calls=(),
            )
            guard.check_batch(plan)
            guard.record_batch(plan, [
                ToolExecutionResult(call_id=call_id, tool_name=tool, status=ToolExecutionStatus.SUCCEEDED, model_content="ok"),
            ])

        # Fifth call A (search) should trigger cycle detection
        call5 = _make_call("c5", "search")
        prep5 = _make_prepared(call5, "fp_search_3")
        plan5 = ToolCallPlan(
            original_calls=(call5,),
            executable_calls=(prep5,),
            rejected_calls=(),
        )

        with pytest.raises(RepetitionError) as exc:
            guard.check_batch(plan5)

        assert "CYCLE" in exc.value.code or "cycle" in str(exc.value).lower()

    def test_tool_failures_tracked(self) -> None:
        """Consecutive failures per tool are tracked."""
        guard = RepetitionGuard(max_same_tool_failures=4)

        for i in range(3):
            tc = _make_call(f"c{i}", "failing_tool")
            prep = _make_prepared(tc, f"fp_{i}")
            plan = ToolCallPlan(
                original_calls=(tc,),
                executable_calls=(prep,),
                rejected_calls=(),
            )
            guard.check_batch(plan)
            guard.record_batch(plan, [
                ToolExecutionResult(
                    call_id=f"c{i}", tool_name="failing_tool",
                    status=ToolExecutionStatus.FAILED, model_content="error",
                ),
            ])

        assert guard.tool_failure_count("failing_tool") == 3

    def test_idempotent_replay_detection(self) -> None:
        """Idempotent replay is detected when same fingerprint succeeds."""
        guard = RepetitionGuard(max_repeated_fingerprint=2)

        tc = _make_call("c1", "read_file", {"path": "/a"})
        prep = _make_prepared(tc, "fp_unique")
        plan = ToolCallPlan(
            original_calls=(tc,),
            executable_calls=(prep,),
            rejected_calls=(),
        )
        guard.check_batch(plan)
        guard.record_batch(plan, [
            ToolExecutionResult(call_id="c1", tool_name="read_file", status=ToolExecutionStatus.SUCCEEDED, model_content="ok"),
        ])

        assert guard.is_idempotent_replay(tool_name="read_file", arguments_fingerprint="fp_unique")
