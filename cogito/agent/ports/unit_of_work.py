# cogito/agent/ports/unit_of_work.py
#
# Unit of Work port for PersistencePhase.
#
# The UoW is a scoped transactional boundary that exposes all six
# persistence repositories.  The PersistencePhase creates a UoW,
# performs all writes through its repositories, and then commits
# or rolls back the entire batch atomically.
#
# Design rules (see persistence-phase-spec §9):
#   - Every PersistencePhase transaction creates exactly one UoW.
#   - Repository methods inside a UoW must NOT call commit/rollback.
#   - Embedding preparation must be done BEFORE the UoW starts.

from __future__ import annotations

from typing import Protocol, Self

from cogito.agent.ports.repositories import (
    CandidateAuditRepositoryPort,
    EmbeddingJobRepositoryPort,
    EventRepositoryPort,
    SessionPersistenceRepositoryPort,
    TurnCommitRepositoryPort,
)
from cogito.agent.ports.repositories_memory import MemoryRepositoryPort


class UnitOfWorkPort(Protocol):
    """Transactional boundary for PersistencePhase.

    Access repositories as properties::

        async with uow_factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="…")
            await uow.events.add_many(…)
            await uow.commit()
    """

    @property
    def sessions(self) -> SessionPersistenceRepositoryPort: ...

    @property
    def events(self) -> EventRepositoryPort: ...

    @property
    def memories(self) -> MemoryRepositoryPort: ...

    @property
    def turn_commits(self) -> TurnCommitRepositoryPort: ...

    @property
    def candidate_audits(self) -> CandidateAuditRepositoryPort: ...

    @property
    def embedding_jobs(self) -> EmbeddingJobRepositoryPort: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactoryPort(Protocol):
    """Factory for creating UnitOfWork instances.

    Each call to ``create()`` must return a fresh UoW backed by a
    fresh transaction state.  This is critical for retry behaviour:
    every retry attempt must use a new UoW.
    """

    def create(self) -> UnitOfWorkPort: ...
