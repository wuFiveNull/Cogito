# cogito/infrastructure/retrieval/preference.py
#
# PreferenceRetrieverAdapter — retrieves user preferences relevant to
# the current query.
#
# Delegates to MemoryRepository to find active "preference" type
# memories for the current user, then applies a simple keyword
# overlap relevance filter.

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
class PreferenceRetrieverAdapter:
    """Preference retriever backed by the memories table.

    Only returns active "preference" type memories for the current
    actor.  Results are scored by simple token overlap with the
    query text.
    """

    _db: AsyncDatabase = field(repr=False)
    name: str = "preference"
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
        rows = await self._repo.get_active_by_type(
            user_id=user_id,
            memory_type="preference",
            limit=limit * 2,
        )
        if not rows:
            return RetrievalBatch(source=self.name, items=())

        # Score by keyword overlap with query text
        query_tokens = _tokenize(query.text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            content = row.get("content", "")
            key = row.get("memory_key", "")
            overlap = _overlap_score(query_tokens, key, content)
            scored.append((overlap, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        items = tuple(
            self._row_to_item(row, score=score)
            for score, row in scored[:limit]
        )
        return RetrievalBatch(source=self.name, items=items)

    def _row_to_item(
        self,
        row: dict[str, Any],
        score: float,
    ) -> RetrievedItem:
        return RetrievedItem(
            item_id=row["id"],
            kind=RetrievedItemKind.PREFERENCE,
            content=row.get("content", ""),
            source=self.name,
            score=score,
            dedupe_key=f"preference:{row['memory_key']}",
        )


def _tokenize(text: str) -> set[str]:
    """Simple tokenisation: split on whitespace and take short Chinese segments."""
    if not text:
        return set()
    tokens: set[str] = set()
    for word in text.lower().split():
        tokens.add(word)
    return tokens


def _overlap_score(
    query_tokens: set[str],
    key: str,
    content: str,
) -> float:
    """Compute a relevance score based on token overlap.

    Returns a value in [0.0, 1.0] where 1.0 means all query tokens
    appear in the key or content.
    """
    if not query_tokens:
        return 0.1  # Low default for empty query

    target_tokens = _tokenize(f"{key} {content}")
    if not target_tokens:
        return 0.0

    overlap = query_tokens & target_tokens
    return len(overlap) / len(query_tokens)
