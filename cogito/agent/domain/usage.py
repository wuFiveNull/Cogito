# cogito/agent/domain/usage.py
#
# Usage and tool-record domain types.
#
# Design rules (see agent-loop-phase-spec §17):
#   - UsageSummary is accumulated across all model rounds in a turn.
#   - ToolExecutionRecord captures one tool call attempt.
#   - Total tokens are always recomputed as input + output, never
#     taken verbatim from a provider that may report inconsistently.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from cogito.agent.domain.tools import ToolExecutionStatus


@dataclass(slots=True)
class UsageSummary:
    """Accumulated token and call counts for one turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    """Immutable record of one tool-call execution.

    ``call_id`` and ``tool_name`` identify the call.
    ``status`` reflects the outcome (succeeded / failed / denied / …).
    ``idempotency_key`` and ``arguments_fingerprint`` are used for
    loop detection and audit — not exposed in public events.
    """

    call_id: str
    tool_name: str
    status: ToolExecutionStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    retryable: bool = False
    idempotency_key: str | None = None
    arguments_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class PersistableToolRecord:
    """Full tool record suitable for PersistencePhase.

    This is a superset of ``ToolExecutionRecord`` that adds the
    sanitised arguments, result, and error message needed to construct
    ``tool_request`` and ``tool_result`` / ``tool_error`` events in
    the database.

    ``ordinal`` is the 0-based position of this call within the
    turn's tool-call sequence.  It is used to assign ``logical_order``
    values in EventDraft.

    ``safe_arguments`` and ``safe_result`` have already passed through
    the sanitizer — they contain no secrets, credentials, or oversized
    payloads.

    ``safe_error_message`` is a user-facing description, never a stack
    trace or internal error detail.
    """

    call_id: str
    ordinal: int
    tool_name: str
    succeeded: bool
    started_at: datetime
    completed_at: datetime
    duration_ms: int | None = None
    safe_arguments: Mapping[str, object] = field(default_factory=dict)
    safe_result: Mapping[str, object] | None = None
    error_code: str | None = None
    safe_error_message: str | None = None
