# cogito/agent/ports/repositories.py

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from cogito.agent.domain.memory import MemoryCandidate, SummaryCandidate
from cogito.agent.domain.preferences import PreferenceCandidate
from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.agent.runtime.persistence.models import (
    CandidateWriteAudit,
    EmbeddingJob,
    PersistedEvent,
    SessionSnapshot,
    TurnCommitRecord,
)


class SessionRepositoryPort(Protocol):
    """Storage for session data."""

    async def get(self, session_id: str) -> SessionState | None:
        ...


class MessageRepositoryPort(Protocol):
    """Storage for conversation messages."""

    async def list_recent(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        ...

    async def save_turn(self, turn: object) -> None:
        ...


class SummaryRepositoryPort(Protocol):
    """Storage for session summaries."""

    async def get(self, session_id: str) -> SessionSummary | None:
        ...

    async def update(
        self,
        *,
        session_id: str,
        candidate: SummaryCandidate,
    ) -> None:
        ...


class UserProfileRepositoryPort(Protocol):
    """Storage for user profile data."""

    async def get(self, actor_id: str) -> UserProfile | None:
        ...


class UserSettingsRepositoryPort(Protocol):
    """Storage for deterministic user settings."""

    async def get(self, actor_id: str) -> UserSettings | None:
        ...


class SessionConfigRepositoryPort(Protocol):
    """Storage for session-level configuration."""

    async def get(self, session_id: str) -> SessionConfig | None:
        ...


class PreferenceRepositoryPort(Protocol):
    """Storage for user preferences."""

    async def list_for_actor(
        self,
        actor_id: str,
    ) -> list[object]:
        ...

    async def apply_candidates(
        self,
        *,
        actor_id: str,
        candidates: list[PreferenceCandidate],
    ) -> None:
        ...


class MemoryRepositoryPort(Protocol):
    """Storage for long-term memories."""

    async def save_candidates(
        self,
        *,
        actor_id: str,
        candidates: list[MemoryCandidate],
    ) -> None:
        ...


# ═══════════════════════════════════════════════════════════════════════
# PersistencePhase Ports
#
# These Ports are used by PersistencePhase and its infrastructure
# adapters.  They are defined alongside the StateLoadPhase ports
# above to keep all repository protocols in one file.
# ═══════════════════════════════════════════════════════════════════════


class SessionPersistenceRepositoryPort(Protocol):
    """Transactional session operations for PersistencePhase."""

    async def create_if_absent(
        self,
        *,
        session_id: str,
        user_id: str,
        now: datetime,
    ) -> None:
        ...

    async def get_for_write(
        self,
        *,
        session_id: str,
    ) -> SessionSnapshot | None:
        ...

    async def advance(
        self,
        *,
        session_id: str,
        expected_version: int,
        consumed_sequences: int,
        last_turn_id: str,
        last_request_id: str,
        last_message_at: datetime,
    ) -> SessionSnapshot:
        ...

    async def update_summary(
        self,
        *,
        session_id: str,
        content: str,
        expected_summary_version: int,
        now: datetime,
    ) -> SessionSnapshot:
        ...


class EventRepositoryPort(Protocol):
    """Event storage for PersistencePhase."""

    async def add_many(
        self,
        events: tuple[PersistedEvent, ...],
    ) -> None:
        ...

    async def get_by_id(self, event_id: str) -> PersistedEvent | None:
        ...


class TurnCommitRepositoryPort(Protocol):
    """Idempotent turn-commit storage."""

    async def get_by_request(
        self,
        *,
        user_id: str,
        request_id: str,
    ) -> TurnCommitRecord | None:
        ...

    async def add(self, record: TurnCommitRecord) -> None:
        ...


class CandidateAuditRepositoryPort(Protocol):
    """Candidate-write-audit storage."""

    async def add_many(
        self,
        audits: tuple[CandidateWriteAudit, ...],
    ) -> None:
        ...


class EmbeddingJobRepositoryPort(Protocol):
    """Embedding-compensation-job storage."""

    async def add_many(
        self,
        jobs: tuple[EmbeddingJob, ...],
    ) -> None:
        ...


# Re-export type aliases for convenience
__all__ = [
    "CandidateAuditRepositoryPort",
    "EmbeddingJobRepositoryPort",
    "EventRepositoryPort",
    "MemoryRepositoryPort",
    "MessageRepositoryPort",
    "PreferenceRepositoryPort",
    "SessionConfigRepositoryPort",
    "SessionPersistenceRepositoryPort",
    "SessionRepositoryPort",
    "SummaryRepositoryPort",
    "TurnCommitRepositoryPort",
    "UserProfileRepositoryPort",
    "UserSettingsRepositoryPort",
]
