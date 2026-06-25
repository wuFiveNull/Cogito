# cogito/llm/embedding.py

from dataclasses import dataclass
from typing import Protocol


class Embedder(Protocol):
    async def embed(
        self,
        text: str,
    ) -> list[float]:
        ...

    async def embed_batch(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        ...

    async def close(self) -> None:
        ...


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: str
    model: str
    base_url: str
    api_key: str
    dimensions: int | None = None
    max_batch_size: int = 10
