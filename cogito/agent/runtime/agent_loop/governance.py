# cogito/agent/runtime/agent_loop/governance.py
#
# Runtime governance — multi-layer context quality control executed
# inside the AgentLoopPhase before each model invocation (Mode 3 from
# the context-management research).
#
# These are pure functions with no side effects.  They operate on
# the model_messages list to:
#   1. Drop orphaned tool-call / tool-result pairs.
#   2. Backfill missing tool results with error stubs.
#   3. Degrade older tool results (truncate).
#   4. Hard-snip history when token limits are exceeded.
#
# Each function is idempotent and safe to call every round.

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from cogito.agent.domain.messages import (
    AssistantMessage,
    ModelMessage,
    ToolMessage,
    UserMessage,
)

logger = logging.getLogger(__name__)


# ── 1. Drop orphaned tool pairs ────────────────────────────────────────


def drop_orphan_tool_pairs(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Remove orphaned tool calls and tool results.

    - An AssistantMessage with tool_calls whose call_ids have no
      corresponding ToolMessage is retained BUT marked in metadata.
    - A ToolMessage whose tool_call_id has no preceding
      AssistantMessage tool_call is removed entirely.
    """
    if not messages:
        return messages

    # Collect all call_ids produced by AssistantMessage tool_calls
    produced_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, AssistantMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                produced_ids.add(tc.call_id)

    # Collect all call_ids consumed by ToolMessages
    consumed_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolMessage):
            consumed_ids.add(msg.tool_call_id)

    # Filter out ToolMessages whose call_id was never produced
    result: list[ModelMessage] = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.tool_call_id not in produced_ids:
            logger.debug(
                "Dropping orphan ToolMessage with call_id=%s",
                msg.tool_call_id,
            )
            continue
        result.append(msg)

    # For orphaned calls without results, update metadata
    orphaned = produced_ids - consumed_ids
    if orphaned:
        for i, msg in enumerate(result):
            if isinstance(msg, AssistantMessage) and msg.tool_calls:
                orphaned_in_msg = [tc for tc in msg.tool_calls if tc.call_id in orphaned]
                if orphaned_in_msg:
                    meta = dict(msg.metadata)
                    meta["orphan_call_ids"] = list(tc.call_id for tc in orphaned_in_msg)
                    result[i] = AssistantMessage(
                        content=msg.content,
                        tool_calls=msg.tool_calls,
                        provider_response_id=msg.provider_response_id,
                        metadata=meta,
                    )

    return result


# ── 2. Backfill missing tool results ───────────────────────────────────


def backfill_missing_tool_results(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Insert stub error ToolMessages for tool calls that have no result.

    Scans for AssistantMessages with tool_calls.  For each tool_call,
    looks for a corresponding ToolMessage *after* the AssistantMessage.
    If not found, inserts a stub error ToolMessage right after the
    AssistantMessage.
    """
    if not messages:
        return messages

    result: list[ModelMessage] = []

    for msg in messages:
        result.append(msg)

        if not isinstance(msg, AssistantMessage) or not msg.tool_calls:
            continue

        # Collect call_ids that appear in ToolMessages already in result
        consumed_ids: set[str] = set()
        for existing in result:
            if isinstance(existing, ToolMessage):
                consumed_ids.add(existing.tool_call_id)

        # Check each tool_call for a matching result
        missing = [tc for tc in msg.tool_calls if tc.call_id not in consumed_ids]
        for tc in missing:
            logger.debug(
                "Backfilling stub ToolMessage for call_id=%s tool=%s",
                tc.call_id,
                tc.tool_name,
            )
            result.append(
                ToolMessage(
                    tool_call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    content='{"error":{"code":"RESULT_MISSING","message":"工具结果缺失"}}',
                    is_error=True,
                    metadata={"kind": "stub", "reason": "backfilled"},
                ),
            )

    return result


# ── 3. Degrade old tool results (full chain) ──────────────────────────


@dataclass(frozen=True)
class DegradeStage:
    """A single stage in the tool-result degradation chain."""

    age_threshold: int      # tool rounds after which this stage applies
    keep_head_lines: int    # lines to keep from the start (0 = none)
    keep_tail_lines: int    # lines to keep from the end (0 = none)
    summary_template: str | None = None  # if set, replaces content with this


# Default degradation chain matching the research document §3 Mode 12:
#   Stage 0 (age < 1):   full content
#   Stage 1 (age < 3):   head + tail 10 lines
#   Stage 2 (age < 10):  single-line summary
#   Stage 3 (age >= 10): removed from list
DEFAULT_DEGRADE_CHAIN: tuple[DegradeStage, ...] = (
    DegradeStage(age_threshold=1, keep_head_lines=0, keep_tail_lines=0),         # full
    DegradeStage(age_threshold=3, keep_head_lines=10, keep_tail_lines=10),       # preview
    DegradeStage(age_threshold=10, keep_head_lines=0, keep_tail_lines=0,
                 summary_template="[{tool_name} result omitted]"),               # summary
)


def _count_tool_rounds(messages: list[ModelMessage]) -> int:
    """Count the number of tool rounds in a message list.

    A tool round = an AssistantMessage with tool_calls followed by
    ToolMessages.  We count the most recent completed round as 0,
    the one before that as 1, etc.
    """
    assistant_indices = [
        i for i, m in enumerate(messages)
        if isinstance(m, AssistantMessage) and m.tool_calls
    ]
    return len(assistant_indices)


def degrade_tool_results(
    messages: list[ModelMessage],
    *,
    keep_recent_n: int = 3,
    max_old_result_chars: int = 500,
    degrade_chain: tuple[DegradeStage, ...] = DEFAULT_DEGRADE_CHAIN,
) -> list[ModelMessage]:
    """Degrade older tool results using a multi-stage chain.

    Each ToolMessage is assigned an age based on how many tool rounds
    have passed since it was produced.  The age determines which
    degradation stage applies.

    Stages (configurable via ``degrade_chain``):
      0 — Full content (age < stage[0].age_threshold)
      1 — Head+tail preview (age < stage[1].age_threshold)
      2 — Single-line summary (age < stage[2].age_threshold)
      3 — Removed from list (age >= stage[-1].age_threshold)

    When ``degrade_chain`` is not provided (or empty), falls back to
    the simple truncation behaviour of ``keep_recent_n`` and
    ``max_old_result_chars``.
    """
    if not messages:
        return messages

    # Count total tool rounds
    total_rounds = _count_tool_rounds(messages)

    # Assign age to each ToolMessage based on its position
    # The last tool round (closest to end) = age 0
    tool_round_seen = 0
    result = list(messages)

    if not degrade_chain:
        # Fallback: simple truncation
        return _simple_degrade_tool_results(
            messages, keep_recent_n=keep_recent_n,
            max_old_result_chars=max_old_result_chars,
        )

    # Walk backwards and assign ages
    current_round = -1
    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        if isinstance(msg, AssistantMessage) and msg.tool_calls:
            current_round += 1  # this round just ended

        if not isinstance(msg, ToolMessage):
            continue

        age = current_round  # 0 = most recent, higher = older
        if age < 0:
            age = 0

        # Find matching stage
        stage = None
        for s in degrade_chain:
            if age < s.age_threshold:
                stage = s
                break
        # If age >= all thresholds, remove the message
        if stage is None:
            result[i] = _make_removed_stub(msg)
            continue

        # Apply stage transformation
        if stage.summary_template is not None:
            summary = stage.summary_template.format(tool_name=msg.tool_name)
            result[i] = ToolMessage(
                tool_call_id=msg.tool_call_id,
                tool_name=msg.tool_name,
                content=summary,
                is_error=msg.is_error,
                metadata={**msg.metadata, "degraded": True, "degrade_stage": "summary"},
            )
        elif stage.keep_head_lines > 0 or stage.keep_tail_lines > 0:
            lines = msg.content.splitlines()
            if len(lines) > stage.keep_head_lines + stage.keep_tail_lines:
                head = lines[:stage.keep_head_lines]
                tail = lines[-stage.keep_tail_lines:] if stage.keep_tail_lines > 0 else []
                preview = "\n".join(head + (["..."] if tail else []) + tail)
                truncated_msg = (
                    preview
                    + f"\n[截断: 原始结果 {len(msg.content)} 字符, "
                    f"{len(lines)} 行]"
                )
            else:
                truncated_msg = msg.content  # already short enough
            result[i] = ToolMessage(
                tool_call_id=msg.tool_call_id,
                tool_name=msg.tool_name,
                content=truncated_msg,
                is_error=msg.is_error,
                metadata={**msg.metadata, "degraded": True, "degrade_stage": "preview"},
            )
        # else stage 0 — keep full

    return result


def _make_removed_stub(msg: ToolMessage) -> ToolMessage:
    """Replace a removed tool result with a minimal stub."""
    return ToolMessage(
        tool_call_id=msg.tool_call_id,
        tool_name=msg.tool_name,
        content=f"[{msg.tool_name} result removed after degradation]",
        is_error=msg.is_error,
        metadata={**msg.metadata, "degraded": True, "degrade_stage": "removed"},
    )


def _simple_degrade_tool_results(
    messages: list[ModelMessage],
    *,
    keep_recent_n: int = 3,
    max_old_result_chars: int = 500,
) -> list[ModelMessage]:
    """Simple truncation-based degradation (fallback when no chain configured)."""
    if not messages:
        return messages

    # Find indices of all ToolMessages (from end to start)
    tool_indices = [
        i for i, msg in enumerate(messages) if isinstance(msg, ToolMessage)
    ]

    # Keep the last keep_recent_n at full length
    recent_indices = set(tool_indices[-keep_recent_n:]) if keep_recent_n > 0 else set()

    result = list(messages)
    for i in tool_indices:
        if i in recent_indices:
            continue  # keep full
        msg = messages[i]
        if not isinstance(msg, ToolMessage):
            continue
        if len(msg.content) <= max_old_result_chars:
            continue  # already short enough

        truncated = msg.content[:max_old_result_chars]
        truncated += f"\n[截断: 原始结果 {len(msg.content)} 字符]"

        result[i] = ToolMessage(
            tool_call_id=msg.tool_call_id,
            tool_name=msg.tool_name,
            content=truncated,
            is_error=msg.is_error,
            metadata={**msg.metadata, "truncated": True, "original_length": len(msg.content)},
        )

    return result


# ── 4. Snip history (hard limit) ───────────────────────────────────────


def estimate_tokens(text: str, *, divisor: float = 4.0) -> int:
    """Rough token estimation based on byte/character length.

    Default divisor of 4.0 is conservative (English + CJK mixed).
    This is intentionally NOT a model-specific tokenizer.
    """
    return max(1, int(len(text) / divisor))


def estimate_messages_tokens(messages: list[ModelMessage]) -> int:
    """Estimate total token count for a list of messages."""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", "") or ""
        total += estimate_tokens(content)
    return total


def snip_history(
    messages: list[ModelMessage],
    hard_limit_tokens: int,
) -> list[ModelMessage]:
    """Hard-truncate history to fit within a token limit.

    Drops non-essential messages from the beginning (oldest first)
    until the estimated token count fits within the limit.

    The first SystemMessage and the last UserMessage (current request)
    are always preserved.
    """
    if not messages:
        return messages

    from cogito.agent.domain.messages import SystemMessage as _SM

    result = list(messages)

    while len(result) > 2:  # at least system + current user
        total = estimate_messages_tokens(result)
        if total <= hard_limit_tokens:
            break

        # Drop the first non-system, non-current-user message
        dropped = False
        for i in range(len(result)):
            if i == 0 and isinstance(result[i], _SM):
                continue  # preserve system message
            if i == len(result) - 1 and isinstance(result[i], UserMessage):
                continue  # preserve current user request
            _ = result.pop(i)
            logger.debug("Snipped history message at index %d", i)
            dropped = True
            break

        if not dropped:
            break  # safety valve

    return result
