-- 0018: Summary rolling — parent_summary_id + input_hash
-- Applied at version 18
--
-- 里程碑 C：支持滚动摘要的父链追踪和输入哈希幂等

ALTER TABLE session_summaries ADD COLUMN parent_summary_id TEXT REFERENCES session_summaries(summary_id);
ALTER TABLE session_summaries ADD COLUMN input_hash        TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_summary_parent
    ON session_summaries(parent_summary_id);
CREATE INDEX IF NOT EXISTS idx_summary_hash
    ON session_summaries(input_hash);
