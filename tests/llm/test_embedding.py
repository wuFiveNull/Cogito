import pytest

from cogito.llm.embedding import EmbeddingProfile


class TestEmbeddingProfile:
    def test_minimal(self):
        profile = EmbeddingProfile(provider="dashscope", model="text-embedding-v3", base_url="https://api.example.com", api_key="sk-xxx")
        assert profile.provider == "dashscope"
        assert profile.model == "text-embedding-v3"
        assert profile.base_url == "https://api.example.com"
        assert profile.api_key == "sk-xxx"

    def test_defaults(self):
        profile = EmbeddingProfile(provider="p", model="m", base_url="https://api.example.com", api_key="sk-xxx")
        assert profile.dimensions is None
        assert profile.max_batch_size == 10

    def test_with_dimensions(self):
        profile = EmbeddingProfile(provider="p", model="m", base_url="https://api.example.com", api_key="sk-xxx", dimensions=1024)
        assert profile.dimensions == 1024
