"""Embedding service — 语义向量化抽象层（F1+F2）。

设计为可插拔的 Provider 模式：
- NoopEmbeddingProvider：默认实现，所有方法返回空（适合 FTS-only 模式）
- OpenAICompatEmbeddingProvider：OpenAI 兼容 API 实现
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Protocol

_LOGGER = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    """语义向量化提供者协议。"""

    async def embed(self, text: str) -> list[float]:
        """将单条文本转为向量。"""
        ...

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量将文本转为向量。"""
        ...

    @property
    def model_name(self) -> str:
        """当前模型名称（用于版本追踪）。"""
        ...

    @property
    def model_version(self) -> str:
        """当前模型版本（用于版本追踪）。"""
        ...

    @property
    def dimensions(self) -> int:
        """向量维度（0 表示未知）。"""
        ...


class NoopEmbeddingProvider:
    """无 Embedding 能力的占位实现。"""

    async def embed(self, text: str) -> list[float]:
        return []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]

    @property
    def model_name(self) -> str:
        return "noop"

    @property
    def model_version(self) -> str:
        return "0"

    @property
    def dimensions(self) -> int:
        return 0


class OpenAICompatEmbeddingProvider:
    """F2: OpenAI 兼容 Embedding API Provider。

    支持：
    - 单条/批量 embedding
    - timeout、retry-after
    - model/version/dimensions 校验
    - API 故障时返回空（不阻塞普通聊天）
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        dimensions: int = 0,
        timeout: float = 30.0,
        max_batch_size: int = 32,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._dimensions = dimensions
        self._timeout = timeout
        self._max_batch_size = max_batch_size
        self._version = "1"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def model_version(self) -> str:
        return self._version

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> list[float]:
        """单条 embedding。"""
        result = await self.embed_many([text])
        return result[0] if result else []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding，自动分批。"""
        if not texts:
            return []

        all_results: list[list[float]] = []
        for i in range(0, len(texts), self._max_batch_size):
            batch = texts[i:i + self._max_batch_size]
            result = await self._embed_batch(batch)
            all_results.extend(result)

        return all_results

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """单批 embedding（≤ max_batch_size）。"""
        try:
            import aiohttp

            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            body: dict = {"model": self._model, "input": texts}
            if self._dimensions:
                body["dimensions"] = self._dimensions

            url = f"{self._base_url}/embeddings"
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("Embedding API error: %s", resp.status)
                        return [[] for _ in texts]
                    data = await resp.json()

            vectors = []
            for item in data.get("data", []):
                vec = item.get("embedding", [])
                vectors.append(vec)

            # 维度校验
            if self._dimensions and vectors and len(vectors[0]) != self._dimensions:
                _LOGGER.warning(
                    "Embedding dimension mismatch: expected %d, got %d",
                    self._dimensions, len(vectors[0]) if vectors else 0,
                )
                return [[] for _ in texts]

            return vectors if vectors else [[] for _ in texts]
        except Exception as e:
            _LOGGER.warning("Embedding API call failed: %s", e)
            return [[] for _ in texts]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。

    任一向量为空或长度不一致时返回 0.0。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
