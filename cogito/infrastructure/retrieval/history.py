# cogito/infrastructure/retrieval/history.py
#
# HistoryRetrieverAdapter — retrieves relevant history events.
#
# Loads recent session events and scores them by keyword overlap
# with the query text.  In a full implementation this could use
# the EventService or a dedicated history index.

from __future__ import annotations

from cogito.agent.domain.retrieval import (
    RetrievalBatch,
    RetrievalQuery,
    RetrievedItem,
    RetrievedItemKind,
)
from cogito.agent.ports.retrieval import RetrieverPort
from cogito.database.connection import AsyncDatabase
from cogito.database.service.event_service import EventService

_HISTORY_LIMIT = 100


class HistoryRetrieverAdapter:
    """History-related event retriever.

    Loads recent events for the current session and scores them
    by keyword overlap.  Results are capped at a generous limit
    server-side, then the adapter returns the top-N by relevance.
    """

    def __init__(
        self,
        db: AsyncDatabase,
        *,
        name: str = "history",
    ) -> None:
        self.name = name
        self._event_service = EventService(db)
        self._history_limit = _HISTORY_LIMIT

    async def retrieve(
        self,
        *,
        query: RetrievalQuery,
        limit: int,
    ) -> RetrievalBatch:
        user_id = query.access.actor_id
        session_id = query.access.session_id

        rows = await self._event_service.get_session_events(
            user_id=user_id,
            session_id=session_id,
            limit=self._history_limit,
        )
        if not rows:
            return RetrievalBatch(source=self.name, items=())

        query_tokens = _tokenize(query.text)
        scored: list[tuple[float, dict[str, object]]] = []

        for row in rows:
            content = row.get("content", "")
            overlap = _token_overlap(query_tokens, content)
            scored.append((overlap, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        items = tuple(
            self._row_to_item(row, score)
            for score, row in scored[:limit]
        )
        return RetrievalBatch(source=self.name, items=items)

    def _row_to_item(
        self,
        row: dict[str, object],
        score: float,
    ) -> RetrievedItem:
        return RetrievedItem(
            item_id=str(row.get("id", "")),
            kind=RetrievedItemKind.HISTORY,
            content=str(row.get("content", "")),
            source=self.name,
            score=score,
            dedupe_key=f"history:{row.get('id', '')}",
        )


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return set(text.lower().split())


def _token_overlap(query_tokens: set[str], content: str) -> float:
    if not query_tokens:
        return 0.05
    target = _tokenize(content)
    if not target:
        return 0.0
    overlap = query_tokens & target
    return len(overlap) / len(query_tokens)
