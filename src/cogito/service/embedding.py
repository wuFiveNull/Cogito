"""Embedding service — 语义向量化抽象层。

设计为可插拔的 Provider 模式：
- NoopEmbeddingProvider：默认实现，所有方法返回空（适合 FTS-only 模式）
- 真实 EmbeddingProvider 可通过配置替换（如 sentence-transformers、OpenAI Embedding API）

Embedding 是派生数据：
- MemoryItem 是权威事实源
- 丢失后可重建
- 不同模型版本不能混合计算
"""

from __future__ import annotations

import math
from typing import Protocol


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


class NoopEmbeddingProvider:
    """无 Embedding 能力的占位实现。

    所有方法返回空值，search_scored 的语义维度得分为 0。
    """

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
