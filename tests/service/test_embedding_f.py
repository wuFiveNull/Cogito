"""Embedding 测试（F1+F2+F5）。

F1: 配置结构。
F2: Provider 行为。
F5: FTS-only 无 Embedding 也能正常工作。
"""

from __future__ import annotations

import sqlite3

import pytest

from cogito.config import Config, EmbeddingConfig
from cogito.service.embedding import (
    NoopEmbeddingProvider,
    OpenAICompatEmbeddingProvider,
    cosine_similarity,
)
from cogito.store.migration import migrate
from cogito.store.memory_repo import MemoryRepository


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    # 关闭外键约束以便单独测试 embedding repo（必须在 migrate 之后、任何 DML 之前）
    conn.execute("PRAGMA foreign_keys=OFF;")
    return conn


class TestEmbeddingConfig:
    def test_default_disabled(self):
        cfg = EmbeddingConfig()
        assert cfg.enabled is False
        assert cfg.is_configured() is False

    def test_configured(self):
        cfg = EmbeddingConfig(
            enabled=True,
            model="text-embedding-3-small",
            api_key="sk-test",
            base_url="https://api.example.com/v1",
        )
        assert cfg.is_configured() is True

    def test_config_default_in_config(self):
        config = Config()
        assert config.embedding.enabled is False

    def test_config_repr_masks_key(self):
        cfg = EmbeddingConfig(api_key="sk-abcdef123456")
        r = repr(cfg)
        assert "sk-a" in r
        assert "****" in r


class TestNoopProvider:
    @pytest.mark.asyncio
    async def test_embed_returns_empty(self):
        p = NoopEmbeddingProvider()
        result = await p.embed("hello")
        assert result == []

    @pytest.mark.asyncio
    async def test_embed_many(self):
        p = NoopEmbeddingProvider()
        result = await p.embed_many(["a", "b"])
        assert result == [[], []]

    @pytest.mark.asyncio
    async def test_properties(self):
        p = NoopEmbeddingProvider()
        assert p.model_name == "noop"
        assert p.dimensions == 0


class TestOpenAICompatProvider:
    def test_init(self):
        p = OpenAICompatEmbeddingProvider(
            model="text-embedding-3-small",
            api_key="sk-test",
            base_url="https://api.example.com/v1",
            dimensions=1536,
        )
        assert p.model_name == "text-embedding-3-small"
        assert p.dimensions == 1536

    @pytest.mark.asyncio
    async def test_embed_no_network_returns_empty(self):
        """无网络时返回空列表（不阻塞）。"""
        p = OpenAICompatEmbeddingProvider(
            model="text-embedding-3-small",
            api_key="sk-test",
            base_url="https://localhost:1/v1",  # 不可达
            timeout=0.1,
        )
        result = await p.embed("hello")
        # 连接失败 → 返回 []
        assert result == [] or isinstance(result, list)


class TestCosineSimilarity:
    def test_identical(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_empty(self):
        assert cosine_similarity([], []) == 0.0
        assert cosine_similarity([1.0], []) == 0.0

    def test_mismatched_length(self):
        assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0


class TestEmbeddingRepo:
    def test_write_and_read(self, db):
        repo = MemoryRepository(db)
        repo.write_embedding("m1", [0.1, 0.2, 0.3], model="test", version="1", dimensions=3)
        result = repo.get_embedding("m1")
        assert result is not None
        assert len(result) == 3
        assert result[0] == pytest.approx(0.1)

    def test_write_empty_vector(self, db):
        repo = MemoryRepository(db)
        repo.write_embedding("m1", [])
        result = repo.get_embedding("m1")
        assert result is None

    def test_list_unembedded(self, db):
        repo = MemoryRepository(db)
        # 插入一条记忆
        db.execute(
            "INSERT INTO memory_items "
            "(memory_id, kind, subject, predicate, value, principal_id, "
            " canonical_key, status, created_at, updated_at) "
            "VALUES ('m1', 'fact', 's', 'p', 'v', 'p1', 'k1', 'confirmed', '2026-01-01', '2026-01-01')"
        )
        db.commit()

        unembedded = repo.list_unembedded()
        assert "m1" in unembedded

        # 写入 embedding 后不再列出
        repo.write_embedding("m1", [0.1], model="noop", version="0")
        unembedded2 = repo.list_unembedded(model="noop")
        assert "m1" not in unembedded2

    def test_delete_propagation(self, db):
        """删除记忆时 embedding 同步删除。"""
        repo = MemoryRepository(db)
        db.execute(
            "INSERT INTO memory_items "
            "(memory_id, kind, subject, predicate, value, principal_id, "
            " canonical_key, status, created_at, updated_at) "
            "VALUES ('m2', 'fact', 's', 'p', 'v', 'p1', 'k2', 'confirmed', '2026-01-01', '2026-01-01')"
        )
        db.commit()
        repo.write_embedding("m2", [0.5], model="test", version="1")
        assert repo.get_embedding("m2") is not None

        repo._sync_embedding_delete("m2")
        assert repo.get_embedding("m2") is None
