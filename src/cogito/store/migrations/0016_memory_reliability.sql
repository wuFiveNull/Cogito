-- 0016: Memory reliability — relations, source ranges, tool UoW
-- Applied at version 16
--
-- 里程碑 A：修复真实手动记忆闭环
-- 1. memory_relations：覆盖/冲突关系追踪
-- 2. memory_sources：消息范围来源表（自动提取）
-- 3. memory_items 默认 scope 字段完善

-- ── 1. memory_relations：可追踪覆盖/冲突/引用关系 ──

CREATE TABLE IF NOT EXISTS memory_relations (
    relation_id    TEXT PRIMARY KEY,
    from_memory_id TEXT NOT NULL REFERENCES memory_items(memory_id),
    to_memory_id   TEXT NOT NULL REFERENCES memory_items(memory_id),
    relation_type  TEXT NOT NULL CHECK(relation_type IN (
        'supersedes', 'contradicts', 'supports', 'refines', 'derived_from', 'corrects'
    )),
    source_type    TEXT NOT NULL DEFAULT 'system',
    source_id      TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    UNIQUE(from_memory_id, to_memory_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_memrel_from
    ON memory_relations(from_memory_id);
CREATE INDEX IF NOT EXISTS idx_memrel_to
    ON memory_relations(to_memory_id);
CREATE INDEX IF NOT EXISTS idx_memrel_type
    ON memory_relations(relation_type);

-- ── 2. memory_sources：关联消息范围（自动提取使用）──

CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id             TEXT PRIMARY KEY REFERENCES memory_items(memory_id),
    source_type           TEXT NOT NULL DEFAULT 'message',
    source_conversation_id TEXT NOT NULL DEFAULT '',
    source_session_id     TEXT NOT NULL DEFAULT '',
    source_from_sequence  INTEGER NOT NULL DEFAULT 0,
    source_to_sequence    INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memsrc_session
    ON memory_sources(source_session_id);

-- ── 3. memory_items 字段补充 ──

ALTER TABLE memory_items ADD COLUMN default_scope   TEXT NOT NULL DEFAULT 'principal-global';
