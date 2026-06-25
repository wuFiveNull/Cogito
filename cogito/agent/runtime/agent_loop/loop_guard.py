# cogito/agent/runtime/agent_loop/loop_guard.py
#
# ToolLoopGuard — detects repeated tool calls and call cycles.
#
# Design rules (see agent-loop-spec §15):
#   - Each tool call is fingerprinted (tool_name + canonical arguments).
#   - If the same fingerprint appears > max_repeated times → RepeatedToolCallError.
#   - If the most recent cycle_detection_window fingerprints contain a
#     repeating pattern of length 2-4 with no result change → CycleError.
#   - Arguments that differ produce different fingerprints → not "repeated"
#     but still participate in cycle detection.

from __future__ import annotations

from dataclasses import dataclass

from cogito.agent.domain.tools import ToolCallPlan, ToolExecutionResult, ToolExecutionStatus
from cogito.agent.runtime.errors import RepeatedToolCallError, ToolCallCycleDetectedError


@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    """One recorded tool call in the guard's history."""

    fingerprint: str
    result_code: str | None  # error_code or "SUCCESS" for succeeded
    result_digest: str  # first 40 chars of model_content (stable summary)


class ToolLoopGuard:
    """Detects repeated and cyclically repeating tool calls across rounds.

    Usage:
        guard = LoopGuardFactory.create(config)
        guard.check_batch(plan)          # before execution
        guard.record_batch(plan, results)  # after execution
    """

    __slots__ = ("_history", "_max_repeated", "_cycle_window")

    def __init__(
        self,
        *,
        max_repeated_fingerprint: int = 2,
        cycle_detection_window: int = 8,
    ) -> None:
        self._history: list[ToolCallObservation] = []
        self._max_repeated = max_repeated_fingerprint
        self._cycle_window = cycle_detection_window

    # ── Check before execution ───────────────────────────────────────

    def check_batch(self, plan: ToolCallPlan) -> None:
        """Check each proposed call against the repeat/cycle guard.

        Must be called before tools execute.  Raises on violation.
        """
        for prepared in plan.executable_calls:
            fp = prepared.arguments_fingerprint
            count = sum(1 for obs in self._history if obs.fingerprint == fp)
            if count >= self._max_repeated:
                raise RepeatedToolCallError(
                    f"Tool call {prepared.call.tool_name} with same arguments "
                    f"repeated {count + 1} times (max {self._max_repeated})",
                    safe_message="工具调用重复次数过多",
                )

        # Cycle detection uses *projected* history (current + proposed)
        projected_fps = [obs.fingerprint for obs in self._history]
        for prepared in plan.executable_calls:
            projected_fps.append(prepared.arguments_fingerprint)

        self._detect_cycle(projected_fps)

    # ── Record after execution ───────────────────────────────────────

    def record_batch(
        self,
        plan: ToolCallPlan,
        results: list[ToolExecutionResult],
    ) -> None:
        """Record the actual outcomes of a batch for future checks."""
        result_by_call_id: dict[str, ToolExecutionResult] = {
            r.call_id: r for r in results
        }

        for prepared in plan.executable_calls:
            result = result_by_call_id.get(prepared.call.call_id)
            code: str | None = None
            digest = ""
            if result is not None:
                code = result.error_code or (
                    "SUCCESS" if result.status is ToolExecutionStatus.SUCCEEDED else result.status
                )
                digest = result.model_content[:40]

            self._history.append(
                ToolCallObservation(
                    fingerprint=prepared.arguments_fingerprint,
                    result_code=code,
                    result_digest=digest,
                ),
            )

        # Prune history to cycle_window size
        if len(self._history) > self._cycle_window * 2:
            self._history = self._history[-(self._cycle_window * 2):]

    # ── Cycle detection ──────────────────────────────────────────────

    def _detect_cycle(self, fingerprints: list[str]) -> None:
        """Check for repeating patterns in the fingerprint sequence."""
        if len(fingerprints) < 4:
            return

        window = fingerprints[-self._cycle_window:]

        # Check periods 2-4
        for period in range(2, min(5, len(window) // 2 + 1)):
            if self._has_period(window, period):
                raise ToolCallCycleDetectedError(
                    f"Detected tool-call cycle of period {period}: {window[-period*2:]}",
                    safe_message="检测到工具调用循环，已停止执行",
                )

    @staticmethod
    def _has_period(seq: list[str], period: int) -> bool:
        """Check if the last (period * 2) elements form at least one full cycle."""
        if len(seq) < period * 2:
            return False

        tail = seq[-period * 2:]
        first_half = tail[:period]
        second_half = tail[period:]

        return first_half == second_half


class DefaultToolLoopGuardFactory:
    """Default factory — creates a ToolLoopGuard from config."""

    def create(self, config: ToolLoopGuardConfig | None = None) -> ToolLoopGuard:
        cfg = config or ToolLoopGuardConfig()
        return ToolLoopGuard(
            max_repeated_fingerprint=cfg.max_repeated_fingerprint,
            cycle_detection_window=cfg.cycle_detection_window,
        )


@dataclass(frozen=True, slots=True)
class ToolLoopGuardConfig:
    max_repeated_fingerprint: int = 2
    cycle_detection_window: int = 8
