-- 0017: Processing watermarks — session-aware + CAS
-- Applied at version 17
--
-- 里程碑 B1：修正 Watermark Schema
-- 重建 processing_watermarks，加入 session_id 到主键 + input_version + CAS 更新
--
-- 因为旧表 PRIMARY KEY 不包含 session_id，需要：
-- 1. 重命名旧表
-- 2. 创建新表
-- 3. 迁移数据（如有）
-- 4. 删除旧表

-- ── 1. 重命名旧表 ──
ALTER TABLE processing_watermarks RENAME TO processing_watermarks_old;

-- ── 2. 创建新表 ──
CREATE TABLE IF NOT EXISTS processing_watermarks (
    processor_type        TEXT NOT NULL,
    conversation_id       TEXT NOT NULL,
    session_id            TEXT NOT NULL,
    processed_upto_sequence INTEGER NOT NULL DEFAULT 0,
    input_version         INTEGER NOT NULL DEFAULT 0,
    version               INTEGER NOT NULL DEFAULT 1,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (processor_type, conversation_id, session_id)
);

-- ── 3. 迁移数据：旧表每行展开到新表（session_id 从旧表获取）──
-- 旧表只有 processor_type + conversation_id + processed_upto + version + updated_at
-- 新表多了 session_id / processed_upto_sequence / input_version
-- 如果旧表有数据，迁移到新表，session_id 使用 '' 占位
INSERT OR IGNORE INTO processing_watermarks (
    processor_type, conversation_id, session_id,
    processed_upto_sequence, input_version, version, updated_at
)
SELECT
    processor_type, conversation_id, '',
    processed_upto, 0, version, updated_at
FROM processing_watermarks_old;

-- ── 4. 删除旧表 ──
DROP TABLE IF EXISTS processing_watermarks_old;

-- ── 5. 索引 ──
CREATE INDEX IF NOT EXISTS idx_watermark_session
    ON processing_watermarks(processor_type, conversation_id, session_id);
