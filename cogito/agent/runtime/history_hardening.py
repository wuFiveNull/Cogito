# cogito/agent/runtime/history_hardening.py
#
# History hardening — runtime message list repair (Mode 9 from the
# context-management research).
#
# These functions fix API invariants on the model message list before
# it is sent to the LLM provider.  They are pure functions with zero
# side effects — no I/O, no DB, no model calls.
#
# Hardening steps:
#   1. Coalesce consecutive messages with the same role.
#   2. Ensure the list starts with a SystemMessage and ends with a
#      UserMessage.
#   3. Repair orphaned tool-call / tool-result pairs (insert stubs
#      for missing partners).
#
# Each step is idempotent and safe to call multiple times per turn.

from __future__ import annotations

import logging

from cogito.agent.domain.messages import (
    AssistantMessage,
    ModelMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)

logger = logging.getLogger(__name__)


def harden_messages(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Fix API invariants for a list of model messages.

    Applies, in order:
      1. Coalesce consecutive same-role messages.
      2. Ensure valid boundaries (system start, user end).
      3. Repair orphaned tool-call / tool-result pairs.

    The input list is NOT mutated; a new list is returned.
    """
    if not messages:
        return messages

    # 1. Coalesce consecutive same-role messages
    result = _coalesce_same_role(messages)

    # 2. Repair orphaned tool pairs — BEFORE boundary check so stubs
    #    are inserted before we look at what starts the list.
    result = _repair_tool_pairs(result)

    # 3. Ensure the list starts with a SystemMessage.
    #    (The "ends with user" invariant is only for initial model calls,
    #    enforced by ContextAssembly — intermediate loop messages may
    #    legitimately end with ToolMessage or AssistantMessage.)
    if result and not isinstance(result[0], SystemMessage):
        logger.warning(
            "First message is %s, prepending default SystemMessage",
            type(result[0]).__name__,
        )
        result.insert(
            0,
            SystemMessage(
                content="You are a helpful AI assistant.",
                metadata={"kind": "system_policy", "auto_prepended": True},
            ),
        )

    return result


# ── Step 1: Coalesce consecutive same-role messages ─────────────────────


def _coalesce_same_role(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Merge consecutive messages that share the same role.

    - Consecutive SystemMessage → merge content with double-newline.
    - Consecutive UserMessage   → merge content with double-newline.
    - Consecutive ToolMessage   → merge content with double-newline.
    - AssistantMessage with tool_calls are NEVER merged (they carry
      structured tool-call state that must remain separate).

    When a text-only AssistantMessage is immediately followed by a
    tool-call AssistantMessage, they are coalesced into a single
    AssistantMessage that carries both text and tool_calls.
    """
    if not messages:
        return []

    result: list[ModelMessage] = [messages[0]]

    for msg in messages[1:]:
        last = result[-1]

        # SystemMessage + SystemMessage → merge
        if isinstance(msg, SystemMessage) and isinstance(last, SystemMessage):
            merged = SystemMessage(
                content=f"{last.content}\n\n{msg.content}",
                metadata={**last.metadata, **msg.metadata} if last.metadata or msg.metadata else {},
            )
            result[-1] = merged
            continue

        # UserMessage + UserMessage → merge
        if isinstance(msg, UserMessage) and isinstance(last, UserMessage):
            merged = UserMessage(
                content=f"{last.content}\n\n{msg.content}",
                metadata={**last.metadata, **msg.metadata} if last.metadata or msg.metadata else {},
            )
            result[-1] = merged
            continue

        # ToolMessage + ToolMessage → merge
        if isinstance(msg, ToolMessage) and isinstance(last, ToolMessage):
            merged = ToolMessage(
                tool_call_id=last.tool_call_id,
                tool_name=last.tool_name,
                content=f"{last.content}\n\n{msg.content}",
                is_error=last.is_error or msg.is_error,
                metadata={**last.metadata, **msg.metadata} if last.metadata or msg.metadata else {},
            )
            result[-1] = merged
            continue

        # Text-only AssistantMessage followed by tool-call AssistantMessage
        if (
            isinstance(msg, AssistantMessage)
            and isinstance(last, AssistantMessage)
            and last.content is not None
            and last.tool_calls is None
            and msg.tool_calls is not None
        ):
            merged = AssistantMessage(
                content=last.content,
                tool_calls=msg.tool_calls,
                provider_response_id=msg.provider_response_id or last.provider_response_id,
                metadata={**last.metadata, **msg.metadata} if last.metadata or msg.metadata else {},
            )
            result[-1] = merged
            continue

        result.append(msg)

    return result


# ── Step 2: Ensure valid boundaries ────────────────────────────────────


def _ensure_valid_boundaries(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Ensure the message list starts with system and ends with user.

    - If the first message is not SystemMessage, prepend a default one.
    - If the last message is not a UserMessage, strip trailing
      non-user messages (but always keep at least a SystemMessage).
    """
    if not messages:
        return messages

    result = list(messages)

    # Must start with SystemMessage
    if not isinstance(result[0], SystemMessage):
        logger.warning(
            "First message is %s, prepending default SystemMessage",
            type(result[0]).__name__,
        )
        result.insert(
            0,
            SystemMessage(
                content="You are a helpful AI assistant.",
                metadata={"kind": "system_policy", "auto_prepended": True},
            ),
        )

    # Must end with UserMessage — strip trailing non-user messages
    # but always keep at least the first message (which is now a SystemMessage).
    while len(result) > 1 and not isinstance(result[-1], UserMessage):
        last = result[-1]
        logger.debug(
            "Stripping trailing %s to satisfy user-end invariant",
            type(last).__name__,
        )
        result.pop()

    return result


# ── Step 3: Repair orphaned tool pairs ─────────────────────────────────


def _repair_tool_pairs(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Fix orphaned tool-call / tool-result pairs.

    An **orphan tool-call** is an AssistantMessage with ``tool_calls``
    whose call_ids have no corresponding ToolMessage in the subsequent
    messages.  A stub error ToolMessage is inserted.

    An **orphan tool-result** is a ToolMessage whose ``tool_call_id``
    does not correspond to a preceding AssistantMessage tool-call.
    Such messages are removed from the list.

    Returns a new list; the input is not mutated.
    """
    if not messages:
        return messages

    # Collect all call_ids produced by AssistantMessage tool_calls,
    # indexed by their position.
    produced_ids: dict[str, int] = {}  # call_id → index in result
    result: list[ModelMessage] = []

    for msg in messages:
        if isinstance(msg, AssistantMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                produced_ids[tc.call_id] = len(result)  # position of the assistant msg
            result.append(msg)
        elif isinstance(msg, ToolMessage):
            # Check if this tool_call_id was produced
            if msg.tool_call_id in produced_ids:
                result.append(msg)
            else:
                logger.debug(
                    "Dropping orphan ToolMessage with call_id=%s",
                    msg.tool_call_id,
                )
        else:
            result.append(msg)

    # Ensure every produced call_id has a matching ToolMessage after it
    # Walk backwards: for each AssistantMessage with tool_calls, check
    # that every call_id appears in subsequent ToolMessages.
    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        if not isinstance(msg, AssistantMessage) or not msg.tool_calls:
            continue

        # Collect call_ids that appear in ToolMessages after this point
        consumed_ids: set[str] = set()
        for j in range(i + 1, len(result)):
            tm = result[j]
            if isinstance(tm, ToolMessage):
                consumed_ids.add(tm.tool_call_id)

        missing = [tc for tc in msg.tool_calls if tc.call_id not in consumed_ids]
        if missing:
            logger.debug(
                "Inserting %d stub ToolMessage(s) for missing tool results",
                len(missing),
            )
            for tc in missing:
                stub = ToolMessage(
                    tool_call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    content='{"error":{"code":"RESULT_MISSING","message":"工具结果缺失"}}',
                    is_error=True,
                    metadata={"kind": "stub", "reason": "orphan_tool_call"},
                )
                result.insert(i + 1 + missing.index(tc), stub)

            # Re-check for consumed IDs after insertion
            consumed_ids.update(tc.call_id for tc in missing)

    return result
