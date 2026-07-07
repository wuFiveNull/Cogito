-- 0015: Embedding + lifecycle management
-- Applied at version 15
--
-- 阶段 8+9：Embedding 混合检索 + 生命周期治理
-- memory_embeddings 是派生数据，MemoryItem 是权威事实源
-- Embedding 丢失后可重建，不同模型版本不混合计算

-- ── 1. memory_embeddings ──

CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id          TEXT PRIMARY KEY REFERENCES memory_items(memory_id),
    embedding_model    TEXT NOT NULL DEFAULT '',
    embedding_version  TEXT NOT NULL DEFAULT '',
    vector             BLOB,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embedding_model
    ON memory_embeddings(embedding_model, embedding_version);

-- ── 2. memory_items 生命周期扩展字段 ──

ALTER TABLE memory_items ADD COLUMN retrieval_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_items ADD COLUMN retrieval_weight  REAL    NOT NULL DEFAULT 1.0;
ALTER TABLE memory_items ADD COLUMN last_retrieved_at TEXT;
ALTER TABLE memory_items ADD COLUMN half_life_days    REAL    NOT NULL DEFAULT 365.0;
