# cogito/agent/runtime/cleanup.py
#
# RuntimeCleanup — hooks that run after every turn regardless of outcome.

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from cogito.agent.runtime.context import TurnContext

logger = logging.getLogger(__name__)


class RuntimeCleanup(Protocol):
    """Cleanup hook that runs after every turn regardless of outcome."""

    async def run(self, ctx: TurnContext) -> None:
        ...


class DefaultRuntimeCleanup:
    """Default cleanup that records completion time."""

    async def run(self, ctx: TurnContext) -> None:
        try:
            ctx.completed_at = datetime.now()
        except Exception:
            logger.exception("RuntimeCleanup failed")


class CompositeCleanup:
    """Runs multiple cleanup hooks in sequence, isolating failures."""

    def __init__(self, *cleanups: RuntimeCleanup) -> None:
        self._cleanups = cleanups

    async def run(self, ctx: TurnContext) -> None:
        for cleanup in self._cleanups:
            try:
                await cleanup.run(ctx)
            except Exception:
                logger.exception("CompositeCleanup: sub-cleanup failed")


class ConsolidationCleanup:
    """Triggers memory consolidation after a successful turn.

    Reads the session's messages from the repository and fires
    consolidation as a fire-and-forget background task.

    Args:
        session_repo: Repository to read conversation messages.
        consolidation: The ConsolidationService to trigger.
        enabled: Set to False to disable (default True).
    """

    def __init__(
        self,
        session_repo: object,
        consolidation: object,
        *,
        enabled: bool = True,
    ) -> None:
        self._session_repo = session_repo
        self._consolidation = consolidation
        self._enabled = enabled

    async def run(self, ctx: TurnContext) -> None:
        if not self._enabled:
            return
        if ctx.status is None or str(ctx.status) != "completed":
            return  # only consolidate completed turns

        session_id = ctx.request.session_id
        if not session_id:
            return

        try:
            from cogito.agent.runtime.memory.consolidation import ConsolidationService

            cons = self._consolidation
            if not isinstance(cons, ConsolidationService):
                return

            # Load session messages from repository
            repo = self._session_repo
            if repo is None or not hasattr(repo, "list_recent"):
                return

            messages = await repo.list_recent(
                session_id=session_id,
                limit=100,  # enough for consolidation check
            )

            if messages and await cons.should_consolidate(session_id, messages):
                import asyncio
                asyncio.ensure_future(cons.consolidate(session_id, messages))

        except Exception:
            logger.warning("Consolidation cleanup hook failed (non-fatal)")
