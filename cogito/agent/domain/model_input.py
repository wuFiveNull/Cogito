# cogito/agent/domain/model_input.py
#
# Domain types for ContextAssemblyPhase — "what the model sees."
#
# These types model the process of turning a TurnContext into a
# structured list of ModelMessage objects ready for model inference.
#
# Design principles:
#   1. Every message is a structured ModelMessage with a role, NOT a flat string.
#   2. Source blocks are first normalised into ContextBlocks, THEN a
#      stable selection algorithm picks which ones fit the token budget.
#   3. The result (ContextAssemblyResult) is written atomically so
#      downstream phases always see a consistent snapshot.
#   4. No model, repository, vector store or external service is called here.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Sequence

from cogito.agent.domain.messages import ModelMessage


# ── Context section classification ──────────────────────────────────────


class ContextSection(StrEnum):
    """Which logical bucket a block of text belongs to.

    Used for priority-based token-budget allocation and for
    diagnostics / observability.
    """

    SYSTEM_POLICY = "system_policy"
    USER_PROFILE = "user_profile"
    USER_SETTINGS = "user_settings"
    SESSION_SUMMARY = "session_summary"
    RETRIEVED_MEMORY = "retrieved_memory"
    RETRIEVED_KNOWLEDGE = "retrieved_knowledge"
    RECENT_HISTORY = "recent_history"
    CURRENT_REQUEST = "current_request"
    AGENT_SELF = "agent_self"
    USER_MEMORY = "user_memory"
    RECENT_CONTEXT = "recent_context"


# ── ContextBlock — a single candidate piece of context ──────────────────


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """A single, self-contained piece of context before budget selection.

    Every candidate piece of text (a user-setting line, a retrieved
    document, a history message, …) is first rendered into a
    ``ContextBlock``.  The budgeter then decides whether it stays.
    """

    block_id: str
    section: ContextSection
    content: str
    priority: int
    required: bool
    estimated_tokens: int
    source_ref: str | None = None
    score: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


# ── DroppedContextBlock — why a block was excluded ──────────────────────


@dataclass(frozen=True, slots=True)
class DroppedContextBlock:
    """Record of a ContextBlock that was dropped during budget selection."""

    block_id: str
    section: ContextSection
    estimated_tokens: int
    reason: str


# ── BudgetSelection — the output of the budgeter ────────────────────────


@dataclass(frozen=True, slots=True)
class BudgetSelection:
    """Result of running the budgeter over a list of candidate blocks."""

    selected: tuple[ContextBlock, ...]
    dropped: tuple[DroppedContextBlock, ...]
    used_tokens: int


# ── HistoryGroup — a logical (user+assistant[+tool]) exchange ────────────


@dataclass(frozen=True, slots=True)
class HistoryGroup:
    """A contiguous group of conversation messages forming one exchange.

    Groups are kept intact during budget selection so that we never
    leave a tool-result orphaned without its corresponding assistant
    tool-call message.
    """

    messages: tuple[ModelMessage, ...]
    estimated_tokens: int
    newest_sequence: int


# ── ContextAssemblyResult — the final result ────────────────────────────


@dataclass(frozen=True, slots=True)
class ContextAssemblyResult:
    """Immutable record of what was assembled and what was dropped.

    This is written into ``TurnContext.context_assembly`` once every
    validation check has passed, so AgentLoopPhase and downstream
    phases have a reliable, consistent view.
    """

    messages: tuple[ModelMessage, ...]
    estimated_input_tokens: int
    max_input_tokens: int
    reserved_output_tokens: int
    selected_block_ids: tuple[str, ...]
    dropped_blocks: tuple[DroppedContextBlock, ...]
    template_version: str
    tokenizer_name: str
