# cogito/infrastructure/retrieval/long_term_memory.py
#
# LongTermMemoryRetrieverAdapter — retrieves long-term memories via
# hybrid search (keyword + vector + RRF fusion + business weighting).
#
# Delegates to the existing MemoryRetriever.hybrid_search() service.

from __future__ import annotations

import logging
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
from cogito.database.service.memory_retriever import MemoryRetriever

logger = logging.getLogger(__name__)


class LongTermMemoryRetrieverAdapter:
    """Long-term memory retriever using hybrid search.

    Uses the existing MemoryRetriever.hybrid_search() which combines:
      - Exact key match
      - FTS5 full-text search
      - LIKE fallback for short queries
      - Semantic FTS (extra query dimension)
      - Vector similarity (if embedding available)

    Results are fused with Weighted RRF and business-weighted scoring
    inside the MemoryRetriever service.
    """

    def __init__(
        self,
        db: AsyncDatabase,
        embedder: EmbeddingPort | None = None,
        *,
        name: str = "long_term_memory",
    ) -> None:
        self.name = name
        self._db = db
        self._retriever = MemoryRetriever(db)
        self._embedder = embedder

    async def retrieve(
        self,
        *,
        query: RetrievalQuery,
        limit: int,
    ) -> RetrievalBatch:
        user_id = query.access.actor_id
        q = query.text.strip()

        # Build embedding vector if we have an embedder
        query_embedding: list[float] | None = None
        if self._embedder and q:
            try:
                embeddings = await self._embedder.embed_many((q,))
                if embeddings:
                    query_embedding = list(embeddings[0].values)
            except Exception:
                logger.warning(
                    "LTM retriever: embedding generation failed, "
                    "skipping vector channel",
                )

        # Split query into tokens for multi-keyword search
        keywords = [q] if q else None
        semantic: list[str] | None = [q] if len(q) >= 3 else None

        results = await self._retriever.hybrid_search(
            user_id=user_id,
            keywords=keywords,
            semantic_queries=semantic,
            query_embedding=query_embedding,
            top_k=limit,
        )

        items = tuple(
            self._row_to_item(row)
            for row in results
        )
        return RetrievalBatch(source=self.name, items=items)

    def _row_to_item(self, row: dict[str, Any]) -> RetrievedItem:
        raw_score = row.get("final_score", row.get("similarity", 0.5))
        if isinstance(raw_score, str):
            raw_score = float(raw_score)
        score = min(max(float(raw_score), 0.0), 1.0)

        memory_type = row.get("memory_type", "fact")
        kind = _memory_type_to_kind(memory_type)

        return RetrievedItem(
            item_id=row["id"],
            kind=kind,
            content=row.get("content", ""),
            source=self.name,
            score=score,
            dedupe_key=f"ltm:{row.get('memory_key', row['id'])}",
        )


def _memory_type_to_kind(memory_type: str) -> RetrievedItemKind:
    """Map DB memory_type to RetrievedItemKind."""
    mapping = {
        "fact": RetrievedItemKind.USER_FACT,
        "preference": RetrievedItemKind.PREFERENCE,
        "rule": RetrievedItemKind.MEMORY,
        "event": RetrievedItemKind.HISTORY,
    }
    return mapping.get(memory_type, RetrievedItemKind.MEMORY)
