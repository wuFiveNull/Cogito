# cogito/agent/tools/context_governor.py
#
# ContextGovernor — manages tool result token budget in model context.
#
# Design rules (see tool-system-spec §15.3):
#   - Removes orphan tool results (no corresponding call).
#   - Compresses old tool results to stay within budget.
#   - Retains recent N full results + system + current user message.
#   - Caps total tool-result characters per turn.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from cogito.agent.domain.messages import ModelMessage, ToolMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContextGovernorConfig:
    inline_soft_limit_chars: int = 12_000
    inline_hard_limit_chars: int = 50_000
    turn_total_limit_chars: int = 120_000
    preview_head_chars: int = 6_000
    preview_tail_chars: int = 3_000
    max_full_results_retained: int = 4


class ContextGovernor:
    """Manages tool-result character budgets in the model context.

    Called between each model round to keep the message list within
    the configured character limits.
    """

    def __init__(
        self,
        config: ContextGovernorConfig | None = None,
    ) -> None:
        self._config = config or ContextGovernorConfig()

    # ── Public API ──────────────────────────────────────────────────────

    def apply(
        self,
        messages: list[ModelMessage],
        *,
        tool_calls_in_round: list[Any],
    ) -> int:
        """Apply context governance to the model messages.

        Steps:
          1. Remove orphan tool results (results without a matching call).
          2. Truncate individual results over hard limit.
          3. Enforce total turn budget (compress oldest results).
          4. Compress very old results to minimal previews.

        Returns the number of characters removed.
        """
        removed = 0

        removed += self._remove_orphan_results(messages, tool_calls_in_round)
        removed += self._truncate_oversized(messages)
        removed += self._enforce_turn_budget(messages)
        removed += self._compress_old_results(messages)

        return removed

    def estimate_total_chars(
        self,
        messages: list[ModelMessage],
    ) -> int:
        """Estimate the total character count of tool results in messages."""
        total = 0
        for msg in messages:
            if isinstance(msg, ToolMessage):
                total += len(msg.content)
        return total

    # ── Internal ────────────────────────────────────────────────────────

    @staticmethod
    def _remove_orphan_results(
        messages: list[ModelMessage],
        tool_calls_in_round: list[Any],
    ) -> int:
        """Remove tool results that have no corresponding call."""
        removed = 0
        call_ids_in_round = {
            getattr(c, "call_id", "") for c in tool_calls_in_round
        }

        i = 0
        while i < len(messages):
            msg = messages[i]
            if isinstance(msg, ToolMessage):
                if msg.tool_call_id not in call_ids_in_round:
                    removed += len(msg.content)
                    messages.pop(i)
                    continue
            i += 1

        return removed

    @staticmethod
    def _truncate_oversized(messages: list[ModelMessage]) -> int:
        """Truncate individual tool results over the hard limit."""
        removed = 0
        return removed  # handled by ResultProcessor before injection

    def _enforce_turn_budget(self, messages: list[ModelMessage]) -> int:
        """Enforce total turn budget by compressing oldest results."""
        total = self.estimate_total_chars(messages)
        if total <= self._config.turn_total_limit_chars:
            return 0

        removed = 0
        tool_result_indices = [
            i for i, m in enumerate(messages)
            if isinstance(m, ToolMessage)
        ]

        keep_count = min(
            self._config.max_full_results_retained,
            len(tool_result_indices),
        )
        compress_indices = tool_result_indices[:-keep_count] if keep_count > 0 else []

        for idx in compress_indices:
            msg = messages[idx]
            if isinstance(msg, ToolMessage) and len(msg.content) > self._config.preview_head_chars:
                preview = (
                    msg.content[:self._config.preview_head_chars]
                    + f"\n... [truncated from {len(msg.content)} chars]"
                )
                removed += len(msg.content) - len(preview)
                messages[idx] = ToolMessage(
                    tool_call_id=msg.tool_call_id,
                    tool_name=msg.tool_name,
                    content=preview,
                    is_error=msg.is_error,
                )

            if self.estimate_total_chars(messages) <= self._config.turn_total_limit_chars:
                break

        return removed

    @staticmethod
    def _compress_old_results(messages: list[ModelMessage]) -> int:
        """Compress very old results to minimal summaries."""
        removed = 0
        all_tool_indices = [
            i for i, m in enumerate(messages) if isinstance(m, ToolMessage)
        ]

        # Keep only the most recent N full results
        keep = 6
        if len(all_tool_indices) > keep:
            for idx in all_tool_indices[:-keep]:
                msg = messages[idx]
                if isinstance(msg, ToolMessage) and len(msg.content) > 200:
                    summary = msg.content[:200] + "\n...[compressed]"
                    removed += len(msg.content) - len(summary)
                    messages[idx] = ToolMessage(
                        tool_call_id=msg.tool_call_id,
                        tool_name=msg.tool_name,
                        content=summary,
                        is_error=msg.is_error,
                    )

        return removed
