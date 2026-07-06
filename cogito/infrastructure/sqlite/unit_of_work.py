# cogito/infrastructure/sqlite/unit_of_work.py
#
# SQLiteUnitOfWork — concrete UnitOfWork for the PersistencePhase.
#
# Opens a BEGIN IMMEDIATE transaction on entry and commits on
# explicit commit().  Rollback happens automatically on exit
# if an exception occurred and the transaction is still open.
#
# All six persistence-phase repositories are lazily instantiated
# and exposed as properties.

from __future__ import annotations

from cogito.database.connection import AsyncDatabase
from cogito.infrastructure.sqlite.connection import SQLiteConnectionFactory
from cogito.infrastructure.sqlite.repositories.candidate_audits import (
    SQLiteCandidateAuditRepository,
)
from cogito.infrastructure.sqlite.repositories.embedding_jobs import (
    SQLiteEmbeddingJobRepository,
)
from cogito.infrastructure.sqlite.repositories.events import (
    SQLiteEventRepository,
)
from cogito.infrastructure.sqlite.repositories.memories import (
    SQLiteMemoryRepository,
)
from cogito.infrastructure.sqlite.repositories.sessions import (
    SQLiteSessionRepository,
)
from cogito.infrastructure.sqlite.repositories.turn_commits import (
    SQLiteTurnCommitRepository,
)


class SQLiteUnitOfWork:
    """Transactional boundary backed by an AsyncDatabase connection.

    Usage::

        factory = SQLiteUnitOfWorkFactory(connection_factory)
        async with factory.create() as uow:
            session = await uow.sessions.get_for_write(session_id="s1")
            await uow.events.add_many(events)
            await uow.commit()

    Repository methods must NOT call commit/rollback — the UoW owns
    the transaction lifecycle.
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db
        self._committed = False

        # Lazy repository cache
        self._sessions: SQLiteSessionRepository | None = None
        self._events: SQLiteEventRepository | None = None
        self._memories: SQLiteMemoryRepository | None = None
        self._turn_commits: SQLiteTurnCommitRepository | None = None
        self._candidate_audits: SQLiteCandidateAuditRepository | None = None
        self._embedding_jobs: SQLiteEmbeddingJobRepository | None = None

    # ── Repository properties (lazy init) ────────────────────────────

    @property
    def sessions(self) -> SQLiteSessionRepository:
        if self._sessions is None:
            self._sessions = SQLiteSessionRepository(self._db)
        return self._sessions

    @property
    def events(self) -> SQLiteEventRepository:
        if self._events is None:
            self._events = SQLiteEventRepository(self._db)
        return self._events

    @property
    def memories(self) -> SQLiteMemoryRepository:
        if self._memories is None:
            self._memories = SQLiteMemoryRepository(self._db)
        return self._memories

    @property
    def turn_commits(self) -> SQLiteTurnCommitRepository:
        if self._turn_commits is None:
            self._turn_commits = SQLiteTurnCommitRepository(self._db)
        return self._turn_commits

    @property
    def candidate_audits(self) -> SQLiteCandidateAuditRepository:
        if self._candidate_audits is None:
            self._candidate_audits = SQLiteCandidateAuditRepository(self._db)
        return self._candidate_audits

    @property
    def embedding_jobs(self) -> SQLiteEmbeddingJobRepository:
        if self._embedding_jobs is None:
            self._embedding_jobs = SQLiteEmbeddingJobRepository(self._db)
        return self._embedding_jobs

    # ── Lifecycle ────────────────────────────────────────────────────

    async def __aenter__(self) -> SQLiteUnitOfWork:
        await self._db.begin_immediate()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        if exc_type is None and not self._committed:
            # Normal exit without explicit commit — auto-commit
            await self._db.commit()
            self._committed = True
        elif exc_type is not None and not self._committed:
            # Exception exit — rollback
            await self._db.rollback()

    async def commit(self) -> None:
        """Explicitly commit the current transaction."""
        await self._db.commit()
        self._committed = True

    async def rollback(self) -> None:
        """Explicitly roll back the current transaction."""
        await self._db.rollback()


class SQLiteUnitOfWorkFactory:
    """Factory that creates SQLiteUnitOfWork instances.

    Each call to ``create()`` returns a new UoW backed by the
    shared AsyncDatabase connection.  In single-user mode this
    is sufficient; in multi-user mode, connection pooling or
    per-session connections would be needed.
    """

    def __init__(self, connection_factory: SQLiteConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def create(self) -> SQLiteUnitOfWork:
        """Create a fresh UnitOfWork.

        The returned UoW is NOT yet entered — the caller must use
        ``async with`` to begin the transaction.

        Each call creates a new Python object; the underlying
        SQLite connection is shared (single-user design).
        """
        # In a multi-user or multi-connection design, we'd call
        # await self._connection_factory.open() here.  For single-user,
        # the factory returns the shared connection every time.
        return SQLiteUnitOfWork(self._connection_factory.db)
