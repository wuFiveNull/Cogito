# cogito/agent/domain/knowledge/enums.py
#
# Enumerations for the knowledge-extraction subdomain.
#
# Design rules (see KnowledgeExtractionPhase-spec §6.1):
#   - ExtractionRunStatus communicates the result of this turn's
#     extraction run — it is NOT the same as TurnStatus.
#   - EvidenceSourceType describes where an evidence reference came from.
#   - AssertionMode captures how strongly the user stated something.
#   - SummaryUpdateMode tells PersistencePhase how to apply the candidate.
#   - SensitivityLevel enables the SensitivityPolicy to REDACT early.

from __future__ import annotations

from enum import StrEnum


class ExtractionRunStatus(StrEnum):
    """Outcome of the knowledge-extraction run for this turn."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    DEGRADED = "degraded"


class EvidenceSourceType(StrEnum):
    """Kind of evidence source for a candidate."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_RESULT = "tool_result"
    SESSION_STATE = "session_state"


class AssertionMode(StrEnum):
    """How the user stated the information."""

    EXPLICIT = "explicit"
    CORRECTION = "correction"
    NEGATION = "negation"
    INFERRED = "inferred"


class KnowledgeScope(StrEnum):
    """Scope of a piece of knowledge."""

    USER = "user"
    SESSION = "session"
    TASK = "task"


class SensitivityLevel(StrEnum):
    """Sensitivity classification of candidate content."""

    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class SummaryUpdateMode(StrEnum):
    """How a summary candidate should be merged with the existing summary."""

    PATCH = "patch"
    REPLACE = "replace"
