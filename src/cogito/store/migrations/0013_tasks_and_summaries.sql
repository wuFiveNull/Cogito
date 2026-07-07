-- 0013: Task infrastructure — watermarks + session_summaries
-- Applied at version 13
--
-- 阶段 5（PR 6）：后台任务、水位和会话摘要
-- 注意：tasks 和 task_attempts 表已在 0001_initial.sql 中创建，
-- 此处补充 lease_version 列 + processing_watermarks + session_summaries。

-- ── 0. 扩展 tasks 表：添加 lease_version（用于乐观锁）──

ALTER TABLE tasks ADD COLUMN lease_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;

-- ── 1. processing_watermarks ──
-- 独立记录各种后台处理的进度（memory_extract、summary、external_sync 等）
-- 不使用统一的 processed=true，每种 processor 独立追踪

CREATE TABLE IF NOT EXISTS processing_watermarks (
    processor_type    TEXT NOT NULL,
    conversation_id   TEXT NOT NULL,
    session_id        TEXT NOT NULL DEFAULT '',
    processed_upto    INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (processor_type, conversation_id)
);

-- ── 2. session_summaries ──
-- 会话摘要：短期上下文，不是 MemoryItem
-- 每条摘要记录其覆盖的消息范围、版本和状态

CREATE TABLE IF NOT EXISTS session_summaries (
    summary_id       TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    covers_from_seq  INTEGER NOT NULL DEFAULT 0,
    covers_to_seq    INTEGER NOT NULL DEFAULT 0,
    summary_version  INTEGER NOT NULL DEFAULT 1,
    content_json     TEXT NOT NULL DEFAULT '{}',
    model_version    TEXT NOT NULL DEFAULT '',
    prompt_version   TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','superseded')),
    created_at       TEXT NOT NULL,
    activated_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_summaries_session
    ON session_summaries(session_id, covers_from_seq, covers_to_seq);
