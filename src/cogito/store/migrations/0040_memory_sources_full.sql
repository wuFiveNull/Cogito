-- 0040: memory_sources full schema (PLAN-13 P13-01)
-- Applied at version 40
--
-- 升级 0016 的初步 memory_sources 为 PLAN-13 §5.1 完整 schema。
-- SQLite 不能 DROP COLUMN / 修改主键，因此：建新表 → 迁数据 → 删旧表 → 重命名。
-- 整件事在一个事务中完成。

-- ── 1. 创建完整 schema 的新表 ──

CREATE TABLE IF NOT EXISTS memory_sources_v2 (
    memory_source_id    TEXT PRIMARY KEY,
    memory_id           TEXT NOT NULL REFERENCES memory_items(memory_id),
    source_type         TEXT NOT NULL DEFAULT 'message'
        CHECK(source_type IN ('message','event','task','connector_item','knowledge_resource','manual')),
    source_id           TEXT NOT NULL DEFAULT '',
    source_revision     TEXT NOT NULL DEFAULT '',
    source_sequence     INTEGER NOT NULL DEFAULT 0,
    evidence_ref        TEXT NOT NULL DEFAULT '',
    evidence_hash       TEXT NOT NULL DEFAULT '',
    trust_label         TEXT NOT NULL DEFAULT 'unverified',
    extraction_id       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    deleted_at          TEXT
);

-- ── 2. 迁移旧数据（旧表可能不存在，用 IF NOT EXISTS 保护）──

INSERT OR IGNORE INTO memory_sources_v2 (
    memory_source_id, memory_id, source_type, source_id,
    source_revision, source_sequence, evidence_ref, evidence_hash,
    trust_label, extraction_id, created_at, deleted_at
)
SELECT
    memory_id,           -- 旧表 memory_id 即主键，直接作为 memory_source_id
    memory_id,
    source_type,
    COALESCE(source_session_id, ''),
    '',
    COALESCE(source_from_sequence, 0),
    COALESCE(source_conversation_id, ''),
    '',
    '',
    '',
    created_at,
    NULL
FROM memory_sources;

-- ── 3. 替换表 ──

DROP TABLE IF EXISTS memory_sources;
ALTER TABLE memory_sources_v2 RENAME TO memory_sources;

-- ── 4. 索引与唯一约束 ──

CREATE UNIQUE INDEX IF NOT EXISTS idx_memsrc_unique
    ON memory_sources(memory_id, source_type, source_id, source_revision, evidence_hash);

CREATE INDEX IF NOT EXISTS idx_memsrc_memory
    ON memory_sources(memory_id);

CREATE INDEX IF NOT EXISTS idx_memsrc_type_id
    ON memory_sources(source_type, source_id);

DROP INDEX IF EXISTS idx_memsrc_session;
