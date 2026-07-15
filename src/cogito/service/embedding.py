"""Embedding service — 语义向量化抽象层（F1+F2）。

设计为可插拔的 Provider 模式：
- NoopEmbeddingProvider：默认实现，所有方法返回空（适合 FTS-only 模式）
- OpenAICompatEmbeddingProvider：基于 openai.OpenAI 客户端的实现
  使用官方客户端而非 raw aiohttp，因为部分云 API（如 SiliconFlow）依赖
  TLS 指纹或代理层过滤，仅接受 OpenAI SDK 的 TLS 握手。
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

    def embed_sync(self, text: str) -> list[float]:
        """同步单条 embedding（PLAN-16 M5 KNOW-06 hybrid retrieval）。

        用于运行中的同步检索路径，避免在 event loop 内使用 asyncio.run()。
        默认实现退化为 embed_many_sync([text])[0]。
        """
        ...

    def embed_many_sync(self, texts: list[str]) -> list[list[float]]:
        """同步批量 embedding（PLAN-16 M5 KNOW-06 hybrid retrieval）。"""
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

    def embed_sync(self, text: str) -> list[float]:
        return []

    def embed_many_sync(self, texts: list[str]) -> list[list[float]]:
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
    """F2: OpenAI 兼容 Embedding API Provider（基于 openai.OpenAI 客户端）。

    使用官方 SDK 而非 raw aiohttp，因为 SiliconFlow 等云 API
    使用 TLS 指纹/代理层过滤，仅接受 OpenAI SDK 的 TLS 握手。
    支持：单条/批量 embedding、timeout、model/version 追踪、API 故障 fail-open。

    也兼容 self-hosted / 无过滤的 OpenAI 兼容服务。
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
        self._client = None  # lazy init (openai.OpenAI is not pickle-safe for multiprocessing)

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=2,
            )
        return self._client

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

        loop = asyncio.get_event_loop()
        all_results: list[list[float]] = []
        for i in range(0, len(texts), self._max_batch_size):
            batch = texts[i : i + self._max_batch_size]
            result = await loop.run_in_executor(None, self._embed_batch_sync, batch)
            all_results.extend(result)

        return all_results

    def embed_sync(self, text: str) -> list[float]:
        """同步单条 embedding（PLAN-16 M5 同步检索路径）。"""
        result = self.embed_many_sync([text])
        return result[0] if result else []

    def embed_many_sync(self, texts: list[str]) -> list[list[float]]:
        """同步批量 embedding（PLAN-16 M5 同步检索路径）。

        用于同步检索路径（ContextBuilder / search_knowledge），
        避免在运行中的 event loop 内使用 asyncio.run()。
        """
        if not texts:
            return []
        # 在 executor 中运行同步 HTTP 调用，不阻塞 event loop
        try:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self._embed_many_sync_direct, texts)
                return future.result()
        except RuntimeError:
            # 无 running loop：直接同步执行
            return self._embed_many_sync_direct(texts)

    def _embed_many_sync_direct(self, texts: list[str]) -> list[list[float]]:
        all_results: list[list[float]] = []
        for i in range(0, len(texts), self._max_batch_size):
            batch = texts[i : i + self._max_batch_size]
            all_results.extend(self._embed_batch_sync(batch))
        return all_results

    def _embed_batch_sync(self, texts: list[str]) -> list[list[float]]:
        """同步单批 embedding（在 executor 中运行）。

        请求格式与 requests 官方示例一致：仅 input + model，
        不传 encoding_format / dimensions，避免部分云 API（如 SiliconFlow）
        因多余参数返回 400。
        """
        try:
            resp = self._get_client().embeddings.create(
                model=self._model,
                input=texts,
            )
            vectors: list[list[float]] = []
            for item in resp.data:
                raw = item.embedding
                if isinstance(raw, str):
                    import base64
                    import struct

                    decoded = base64.b64decode(raw)
                    n = len(decoded) // 4
                    vec = list(struct.unpack(f">{n}f", decoded))
                elif isinstance(raw, (list, tuple)):
                    vec = [float(x) for x in raw]
                else:
                    vec = []
                vectors.append(vec)

            # 维度校验（软：仅警告）
            if self._dimensions and vectors and vectors[0] and len(vectors[0]) != self._dimensions:
                _LOGGER.warning(
                    "Embedding dimension mismatch: expected %d, got %d",
                    self._dimensions,
                    len(vectors[0]),
                )

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
