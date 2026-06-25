# cogito/infrastructure/retrieval/keyword.py
#
# KeywordRetrieverAdapter — keyword/BM25/full-text search via FTS5.
#
# Delegates to MemoryRepository (FTS5 trigram for ≥3 chars,
# LIKE fallback for shorter queries).

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cogito.agent.domain.retrieval import (
    RetrievalBatch,
    RetrievalQuery,
    RetrievedItem,
    RetrievedItemKind,
)
from cogito.agent.ports.retrieval import RetrieverPort
from cogito.database.connection import AsyncDatabase
from cogito.database.repository.memories import MemoryRepository


@dataclass(slots=True)
class KeywordRetrieverAdapter:
    """Keyword retriever backed by SQLite FTS5.

    Queries are routed to FTS5 (≥3 chars) or LIKE (<3 chars),
    based on the query text length.
    """

    _db: AsyncDatabase = field(repr=False)
    name: str = "keyword"
    _repo: MemoryRepository | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._repo is None:
            object.__setattr__(self, "_repo", MemoryRepository(self._db))

    async def retrieve(
        self,
        *,
        query: RetrievalQuery,
        limit: int,
    ) -> RetrievalBatch:
        user_id = query.access.actor_id
        q = query.text.strip()
        if not q:
            return RetrievalBatch(source=self.name, items=())

        rows: list[dict[str, Any]]
        if len(q) < 3:
            rows = await self._repo.search_like(user_id, q, limit=limit)
        else:
            from cogito.database.utils import utcnow
            query_time = utcnow()
            rows = await self._repo.search_fts(user_id, q, query_time, limit=limit)

        items = tuple(self._row_to_item(r) for r in rows)
        return RetrievalBatch(source=self.name, items=items)

    def _row_to_item(self, row: dict[str, Any]) -> RetrievedItem:
        return RetrievedItem(
            item_id=row["id"],
            kind=RetrievedItemKind.DOCUMENT,
            content=row.get("content", ""),
            source=self.name,
            score=1.0 - abs(row.get("lexical_rank", 0.0)),
            dedupe_key=f"keyword:{row['id']}",
        )
