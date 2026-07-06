# cogito/infrastructure/sqlite/connection.py
#
# SQLiteConnectionFactory — provides AsyncDatabase connections
# for the PersistencePhase Unit of Work.
#
# In the single-user Cogito scenario, a single connection is shared
# across the application.  The factory returns the same AsyncDatabase
# instance so that UoW operations see a consistent transaction state.

from __future__ import annotations

from cogito.database.connection import AsyncDatabase


class SQLiteConnectionFactory:
    """Factory for AsyncDatabase connections used by SQLiteUnitOfWork.

    For the single-user personal-agent case, this holds one shared
    connection.  A future multi-user variant could return a connection
    per session_id.
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def open(self) -> AsyncDatabase:
        """Return a connection to use within a UnitOfWork scoped transaction.

        The caller (UoW) owns the transaction lifecycle via
        ``begin_immediate`` / ``commit`` / ``rollback``.
        """
        return self._db

    @property
    def db(self) -> AsyncDatabase:
        return self._db
