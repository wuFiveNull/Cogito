# cogito/agent/runtime/agent_loop/usage_accumulator.py
#
# UsageAccumulator — aggregates token usage from model calls and tool calls.
#
# Design rules (see agent-loop-spec §17):
#   - input_tokens and output_tokens are accumulated separately.
#   - total_tokens is always recomputed as input + output.
#   - Each model response's usage overwrites the running max per round.
#   - Tool calls are counted once per executed call (including denied).

from __future__ import annotations

from cogito.agent.domain.model import ModelRoundOutput
from cogito.agent.domain.usage import UsageSummary


class UsageAccumulator:
    """Aggregates usage data across multiple model rounds in a turn."""

    __slots__ = ("_input", "_output", "_model_calls", "_tool_calls")

    def __init__(self) -> None:
        self._input: int = 0
        self._output: int = 0
        self._model_calls: int = 0
        self._tool_calls: int = 0

    def add_model_round(self, output: ModelRoundOutput) -> None:
        self._input += output.input_tokens
        self._output += output.output_tokens
        self._model_calls += 1

    def add_tool_call(self) -> None:
        self._tool_calls += 1

    def snapshot(self) -> UsageSummary:
        total = self._input + self._output
        return UsageSummary(
            input_tokens=self._input,
            output_tokens=self._output,
            total_tokens=total,
            model_calls=self._model_calls,
            tool_calls=self._tool_calls,
        )
