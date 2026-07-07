-- 0012: Memory MVP — 扩展 memory_items 表
-- Applied at version 12
--
-- 阶段 1：长期记忆持久化
-- 保留现有字段，新增 principal_id、scope_type/scope_id、canonical_key、
-- explicitness、importance、version、updated_at、deleted_at 等。
--
-- 不增加 active/superseded/archived 状态（由关系和 valid_to 表达）。

-- ── 1. 扩展 memory_items ──

ALTER TABLE memory_items ADD COLUMN principal_id          TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_items ADD COLUMN scope_type            TEXT NOT NULL DEFAULT '' CHECK(scope_type IN ('','global','user','conversation','session','task'));
ALTER TABLE memory_items ADD COLUMN scope_id              TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_items ADD COLUMN canonical_key         TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_items ADD COLUMN explicitness          TEXT NOT NULL DEFAULT '' CHECK(explicitness IN ('','explicit_user_statement','confirmed_inference','model_inference','external_source','system_generated'));
ALTER TABLE memory_items ADD COLUMN importance             REAL NOT NULL DEFAULT 0.5 CHECK(importance >= 0.0 AND importance <= 1.0);
ALTER TABLE memory_items ADD COLUMN confirmation_method   TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_items ADD COLUMN confirmed_by           TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_items ADD COLUMN confirmed_at           TEXT;
ALTER TABLE memory_items ADD COLUMN version                INTEGER NOT NULL DEFAULT 1;
ALTER TABLE memory_items ADD COLUMN updated_at             TEXT;
ALTER TABLE memory_items ADD COLUMN deleted_at             TEXT;

-- ── 2. 新增索引 ──

CREATE INDEX IF NOT EXISTS idx_memory_principal_status_kind
    ON memory_items(principal_id, status, kind);

CREATE INDEX IF NOT EXISTS idx_memory_principal_scope_status
    ON memory_items(principal_id, scope_type, scope_id, status);

CREATE INDEX IF NOT EXISTS idx_memory_canonical_key
    ON memory_items(principal_id, canonical_key);

CREATE INDEX IF NOT EXISTS idx_memory_source
    ON memory_items(source_type, source_id);

CREATE INDEX IF NOT EXISTS idx_memory_supersedes
    ON memory_items(supersedes_id);
