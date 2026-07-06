# cogito/agent/tools/repetition_guard.py
#
# RepetitionGuard — detects and blocks repetitive tool call patterns.
#
# Design rules (see tool-system-spec §10.4):
#   - Same tool + fingerprint repeated N times → block.
#   - Tool call cycle detection (A-B-A-B pattern).
#   - Same-tool consecutive failure threshold.
#   - Idempotent tools with same args can return cached result.

from __future__ import annotations

import logging
from collections import defaultdict
from hashlib import sha256
from typing import Mapping

from cogito.agent.domain.tools import ToolCall, ToolCallPlan, ToolExecutionResult, ToolExecutionStatus

logger = logging.getLogger(__name__)


class RepetitionGuard:
    """Detects and blocks repetitive or cyclic tool call patterns.

    Tracks per-turn state:
      - Fingerprint hit counts (tool_name + canonical args).
      - Consecutive failures per tool.
      - Call sequence for cycle detection.

    The guard is instantiated once per AgentLoop turn and carries
    no state between turns.
    """

    def __init__(
        self,
        *,
        max_repeated_fingerprint: int = 2,
        max_same_tool_failures: int = 4,
        cycle_window: int = 8,
    ) -> None:
        self._max_repeated_fingerprint = max_repeated_fingerprint
        self._max_same_tool_failures = max_same_tool_failures
        self._cycle_window = cycle_window

        # Per-turn state
        self._fingerprint_hits: dict[str, int] = defaultdict(int)
        self._tool_failures: dict[str, int] = defaultdict(int)
        self._call_sequence: list[str] = []

    # ── Public API ──────────────────────────────────────────────────────

    def check_batch(
        self,
        plan: ToolCallPlan,
    ) -> None:
        """Check a batch of tool calls before execution.

        Raises ``RepetitionError`` if the batch violates any guard.
        """
        for prepared in plan.executable_calls:
            self._check_fingerprint(prepared.arguments_fingerprint)
            self._check_cycle(prepared.call.tool_name)

        # Rejected calls don't get guard checks — they already failed.

    def record_batch(
        self,
        plan: ToolCallPlan,
        results: list[ToolExecutionResult],
    ) -> None:
        """Record the results of a batch for future guard checks."""
        result_map = {r.call_id: r for r in results}

        for prepared in plan.executable_calls:
            fp = prepared.arguments_fingerprint

            result = result_map.get(prepared.call.call_id)
            if result is not None:
                if result.status is ToolExecutionStatus.SUCCEEDED:
                    self._fingerprint_hits[fp] += 1
                    self._tool_failures[prepared.call.tool_name] = 0
                else:
                    self._tool_failures[prepared.call.tool_name] += 1

            self._call_sequence.append(prepared.call.tool_name)

    def is_idempotent_replay(
        self,
        *,
        tool_name: str,
        arguments_fingerprint: str,
    ) -> bool:
        """Check if an idempotent tool call has been made before with the same args."""
        return self._fingerprint_hits.get(arguments_fingerprint, 0) > 0

    # ── Internal ────────────────────────────────────────────────────────

    def _check_fingerprint(self, fingerprint: str) -> None:
        """Check if a fingerprint has been repeated too many times."""
        count = self._fingerprint_hits.get(fingerprint, 0)
        if count >= self._max_repeated_fingerprint:
            raise RepetitionError(
                f"Tool call fingerprint repeated {count} times "
                f"(max {self._max_repeated_fingerprint})",
                code="REPEATED_TOOL_CALL",
            )

    def _check_cycle(self, tool_name: str) -> None:
        """Detect A-B-A-B cycles in the recent call sequence."""
        if len(self._call_sequence) < 4:
            return

        recent = self._call_sequence[-self._cycle_window:] + [tool_name]

        # Check for 4-cycle pattern: A-B-A-B
        if len(recent) >= 4:
            if (recent[-4] == recent[-2] and recent[-3] == recent[-1]
                    and recent[-4] != recent[-3]):
                raise RepetitionError(
                    f"Tool call cycle detected: "
                    f"{recent[-4]} → {recent[-3]} → "
                    f"{recent[-2]} → {recent[-1]}",
                    code="TOOL_CALL_CYCLE_DETECTED",
                )

    def tool_failure_count(self, tool_name: str) -> int:
        """Get the consecutive failure count for a tool."""
        return self._tool_failures.get(tool_name, 0)


class RepetitionError(RuntimeError):
    """Raised when a tool call is blocked by the repetition guard."""

    def __init__(self, message: str, *, code: str = "REPETITION_ERROR") -> None:
        super().__init__(message)
        self.code = code
