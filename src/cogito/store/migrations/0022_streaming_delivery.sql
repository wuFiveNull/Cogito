-- 0022: 流式投递字段 (Plan 05: placeholder / edit / finish)
-- 新增支持"先占位 → 增量编辑 → 最终定稿"的投递能力。
-- 所有新列均有默认值，迁移幂等。

ALTER TABLE deliveries ADD COLUMN content_mode TEXT NOT NULL DEFAULT 'final';
ALTER TABLE deliveries ADD COLUMN final_message_id TEXT;
ALTER TABLE deliveries ADD COLUMN stream_status TEXT;
ALTER TABLE deliveries ADD COLUMN degradation_mode TEXT;
ALTER TABLE deliveries ADD COLUMN last_confirmed_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deliveries ADD COLUMN policy_json TEXT;
ALTER TABLE deliveries ADD COLUMN metrics_json TEXT;

ALTER TABLE delivery_attempts ADD COLUMN last_confirmed_revision INTEGER NOT NULL DEFAULT 0;
