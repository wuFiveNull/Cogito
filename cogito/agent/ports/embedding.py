# cogito/agent/ports/embedding.py
#
# Embedding port for PersistencePhase.
#
# The PersistencePhase uses this port to compute embedding vectors
# outside the SQLite write transaction.  Embedding vectors are
# stored in ``memories.embedding`` and recoverable via
# ``embedding_jobs`` if the embedder is unavailable.

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """Result of embedding one text string."""

    model: str
    dimensions: int
    values: tuple[float, ...]


class EmbeddingPort(Protocol):
    """Embedding service for PersistencePhase.

    ``embed_many`` accepts a tuple of text strings and returns a
    tuple of ``EmbeddingVector`` results with the same length and
    order as the input.

    Implementations may throw on network errors; the caller
    (PersistencePhase) treats failures as non-fatal — it creates
    ``embedding_jobs`` for later retry instead of aborting the
    entire turn.
    """

    async def embed_many(
        self,
        texts: tuple[str, ...],
    ) -> tuple[EmbeddingVector, ...]:
        ...
