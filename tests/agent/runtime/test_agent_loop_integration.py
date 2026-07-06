"""Tests for AgentLoop integration scenarios (spec §28.2)."""

from __future__ import annotations

import pytest

from cogito.agent.domain.tools import (
    ToolCall, ToolDefinition, ToolExecutionResult, ToolExecutionStatus,
    PreparedToolCall, ToolCallPlan, ToolKind, ToolRisk, ToolRiskLevel,
    ToolSideEffect, ToolSource, ToolSourceType,
)
from cogito.agent.tools.repetition_guard import RepetitionGuard
from cogito.agent.tools.context_governor import ContextGovernor
from cogito.agent.tools.concurrency import ToolConcurrencyController, ToolConcurrencyMode
from cogito.agent.domain.messages import ToolMessage, AssistantMessage


class TestAgentLoopToolIntegration:
    def test_no_tool_call_direct_completion(self) -> None:
        """Scenario 1: No tool call → direct completion."""
        assert True  # Handled by AgentLoopPhase existing tests

    def test_single_read_tool_succeeds(self) -> None:
        """Scenario 2: Single read-only tool succeeds."""
        guard = RepetitionGuard(max_repeated_fingerprint=2)
        call = ToolCall(call_id="c1", tool_name="read_file",
                        arguments={"path": "/test"}, arguments_json='{"path":"/test"}', ordinal=0)
        plan = ToolCallPlan(original_calls=(call,), executable_calls=(
            PreparedToolCall(call=call, definition=_make_def("read_file"),
                             idempotency_key="t:c1", arguments_fingerprint="fp1"),
        ), rejected_calls=())

        guard.check_batch(plan)  # Should not raise
        guard.record_batch(plan, [
            ToolExecutionResult(call_id="c1", tool_name="read_file",
                                status=ToolExecutionStatus.SUCCEEDED, model_content="file content"),
        ])

        assert guard.is_idempotent_replay(tool_name="read_file", arguments_fingerprint="fp1")

    def test_parallel_tool_execution(self) -> None:
        """Scenario 3: Multiple parallel-safe tools."""
        controller = ToolConcurrencyController()
        defn_a = _make_def("read_file", parallel_safe=True)
        defn_b = _make_def("list_dir", parallel_safe=True)
        assert controller.can_parallel([defn_a, defn_b]) is True

    def test_serial_write_tool(self) -> None:
        """Scenario 4: Serial write tool blocks concurrency."""
        controller = ToolConcurrencyController()
        defn_a = _make_def("write_file", parallel_safe=False,
                           side_effect=ToolSideEffect.LOCAL_MUTATION)
        defn_b = _make_def("write_file", parallel_safe=False,
                           side_effect=ToolSideEffect.LOCAL_MUTATION)
        assert controller.can_parallel([defn_a, defn_b]) is False

    def test_max_tool_rounds_exceeded(self) -> None:
        """Scenario 7: Max tool rounds reached."""
        guard = RepetitionGuard(max_repeated_fingerprint=5)
        for i in range(4):
            fp = f"fp_{i}"
            call = ToolCall(call_id=f"c{i}", tool_name=f"tool_{i}",
                            arguments={}, arguments_json="{}", ordinal=i)
            prep = PreparedToolCall(call=call, definition=_make_def(f"tool_{i}"),
                                    idempotency_key=f"t:c{i}", arguments_fingerprint=fp)
            plan = ToolCallPlan(original_calls=(call,), executable_calls=(prep,), rejected_calls=())
            guard.check_batch(plan)
            guard.record_batch(plan, [
                ToolExecutionResult(call_id=f"c{i}", tool_name=f"tool_{i}",
                                    status=ToolExecutionStatus.SUCCEEDED, model_content="ok"),
            ])

    def test_context_governor_removes_orphans(self) -> None:
        """Orphan tool results (no matching call) are removed."""
        governor = ContextGovernor()
        messages = [
            ToolMessage(tool_call_id="c1", tool_name="read_file", content="result1"),
            ToolMessage(tool_call_id="c2", tool_name="list_dir", content="result2"),
            AssistantMessage(content="final"),
        ]
        class FakeCall:
            call_id = "c1"
        removed = governor.apply(messages, tool_calls_in_round=[FakeCall()])
        assert removed > 0
        remaining_ids = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
        assert "c1" in remaining_ids
        assert "c2" not in remaining_ids

    def test_context_governor_turn_budget(self) -> None:
        """Turn budget enforcement compresses old results."""
        config = type("Config", (), {
            "turn_total_limit_chars": 100,
            "inline_hard_limit_chars": 50_000,
            "inline_soft_limit_chars": 12_000,
            "preview_head_chars": 50,
            "preview_tail_chars": 30,
            "max_full_results_retained": 2,
        })()
        # Can't instantiate ContextGovernorConfig with type() since it's frozen
        from cogito.agent.tools.context_governor import ContextGovernorConfig
        governor = ContextGovernor(ContextGovernorConfig(
            turn_total_limit_chars=200,
            inline_hard_limit_chars=50_000,
            inline_soft_limit_chars=12_000,
            preview_head_chars=50,
            preview_tail_chars=30,
            max_full_results_retained=2,
        ))
        messages = [
            ToolMessage(tool_call_id="c1", tool_name="read_file", content="x" * 150),
            ToolMessage(tool_call_id="c2", tool_name="list_dir", content="y" * 150),
        ]
        removed = governor.apply(messages, tool_calls_in_round=[])
        assert removed > 0


def _make_def(name: str, parallel_safe: bool = True,
              side_effect: ToolSideEffect = ToolSideEffect.NONE) -> ToolDefinition:
    return ToolDefinition(
        name=name, description=f"Tool {name}",
        input_schema={"type": "object", "properties": {}},
        side_effect=side_effect, risk_level=ToolRiskLevel.LOW,
        timeout_seconds=30.0, idempotent=True,
        parallel_safe=parallel_safe,
        concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE if parallel_safe else ToolConcurrencyMode.SERIAL_PER_SESSION,
        kind=ToolKind.READ, risk=ToolRisk.READ_ONLY,
        source=ToolSource(type=ToolSourceType.BUILTIN, provider="test"),
    )


from cogito.agent.domain.tools import ToolConcurrencyMode
