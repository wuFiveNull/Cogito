"""Tests for EmbeddingService — 插拔式 provider + 余弦相似度。"""

import pytest

from cogito.service.embedding import NoopEmbeddingProvider, cosine_similarity


class TestNoopEmbeddingProvider:
    @pytest.mark.asyncio
    async def test_embed_returns_empty(self):
        provider = NoopEmbeddingProvider()
        result = await provider.embed("hello world")
        assert result == []

    @pytest.mark.asyncio
    async def test_embed_many_returns_empty_lists(self):
        provider = NoopEmbeddingProvider()
        results = await provider.embed_many(["a", "b", "c"])
        assert len(results) == 3
        assert all(r == [] for r in results)

    def test_model_name(self):
        provider = NoopEmbeddingProvider()
        assert provider.model_name == "noop"

    def test_model_version(self):
        provider = NoopEmbeddingProvider()
        assert provider.model_version == "0"


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == 1.0

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == 0.0

    def test_partial_similarity(self):
        a = [1.0, 1.0]
        b = [1.0, 0.0]
        result = cosine_similarity(a, b)
        assert 0.5 < result < 1.0

    def test_empty_vector_returns_zero(self):
        assert cosine_similarity([], [1.0]) == 0.0
        assert cosine_similarity([1.0], []) == 0.0

    def test_mismatched_length_returns_zero(self):
        assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0

    def test_zero_vector(self):
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        assert cosine_similarity(a, b) == 0.0
