# cogito/infrastructure/retrieval/vector.py
#
# VectorRetrieverAdapter — semantic similarity search via embeddings.
#
# Generates a query embedding via EmbeddingPort, then compares against
# stored embeddings in the memories table (application-level cosine
# similarity).  Falls back to empty results if no embedder is available.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from cogito.agent.domain.retrieval import (
    RetrievalBatch,
    RetrievalQuery,
    RetrievedItem,
    RetrievedItemKind,
)
from cogito.agent.ports.embedding import EmbeddingPort
from cogito.agent.ports.retrieval import RetrieverPort
from cogito.database.connection import AsyncDatabase
from cogito.database.repository.memories import MemoryRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VectorRetrieverAdapter:
    """Vector similarity retriever backed by stored embeddings.

    Requires an EmbeddingPort to generate query embeddings.
    Without one, returns empty results gracefully.
    """

    _db: AsyncDatabase = field(repr=False)
    _embedder: EmbeddingPort | None = field(repr=False)
    name: str = "vector"
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
        if self._embedder is None:
            logger.debug("VectorRetriever: no embedder available, returning empty")
            return RetrievalBatch(source=self.name, items=())

        from cogito.database.utils import utcnow
        from cogito.database.service.memory_retriever import (
            deserialize_embedding,
            cosine_similarity,
        )

        user_id = query.access.actor_id
        query_time = utcnow()

        # Generate query embedding
        try:
            embeddings = await self._embedder.embed_many((query.text,))
        except Exception:
            logger.exception("VectorRetriever: embedding generation failed")
            return RetrievalBatch(source=self.name, items=())

        if not embeddings:
            return RetrievalBatch(source=self.name, items=())

        query_vector = list(embeddings[0].values)

        # Load candidate embeddings from DB
        candidates = await self._repo.get_active_embeddings(user_id, query_time)
        if not candidates:
            return RetrievalBatch(source=self.name, items=())

        # Score in Python
        scored: list[tuple[float, dict[str, Any]]] = []
        for c in candidates:
            emb_bytes: bytes | None = c.get("embedding")
            if emb_bytes is None:
                continue
            emb = deserialize_embedding(emb_bytes)
            if len(emb) != len(query_vector):
                continue
            sim = cosine_similarity(emb, query_vector)
            scored.append((sim, c))

        scored.sort(key=lambda x: x[0], reverse=True)

        items = tuple(
            self._row_to_item(row, score=sim)
            for sim, row in scored[:limit]
        )
        return RetrievalBatch(source=self.name, items=items)

    def _row_to_item(
        self,
        row: dict[str, Any],
        score: float,
    ) -> RetrievedItem:
        return RetrievedItem(
            item_id=row["id"],
            kind=RetrievedItemKind.DOCUMENT,
            content=row.get("content", ""),
            source=self.name,
            score=min(max(score, 0.0), 1.0),
            dedupe_key=f"vector:{row['id']}",
        )
