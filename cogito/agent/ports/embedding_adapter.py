# cogito/agent/ports/embedding_adapter.py
#
# EmbeddingPortAdapter — bridges llm.Embedder → ports.EmbeddingPort.
#
# The Cogito LLM layer defines an Embedder protocol with embed()
# and embed_batch().  The ports layer defines EmbeddingPort with
# embed_many().  This adapter bridges the two.

from __future__ import annotations

from cogito.agent.ports.embedding import EmbeddingPort, EmbeddingVector
from cogito.llm.embedding import Embedder, EmbeddingProfile


class EmbeddingPortAdapter:
    """Wraps an ``Embedder`` (LLM layer) as an ``EmbeddingPort``.

    Usage::

        embedder = build_embedder(config)  # returns Embedder
        port = EmbeddingPortAdapter(embedder)
        vectors = await port.embed_many(("text1", "text2"))
    """

    def __init__(
        self,
        embedder: Embedder,
        profile: EmbeddingProfile | None = None,
    ) -> None:
        self._embedder = embedder
        self._profile = profile

    async def embed_many(
        self,
        texts: tuple[str, ...],
    ) -> tuple[EmbeddingVector, ...]:
        if not texts:
            return ()

        # embed_batch expects list[str]
        results = await self._embedder.embed_batch(list(texts))

        model = self._profile.model if self._profile else "unknown"
        dimensions = self._profile.dimensions if self._profile else 0

        return tuple(
            EmbeddingVector(
                model=model,
                dimensions=dimensions or len(vec),
                values=tuple(vec),
            )
            for vec in results
        )

    async def close(self) -> None:
        await self._embedder.close()
