-- 0028: connector_items 增加 topic 列（MCP handler 入表时写入，供 proactive
-- candidate 投影 + digest 按 topic 分桶使用）。
-- 展开式变更，不影响既有数据。

ALTER TABLE connector_items ADD COLUMN topic TEXT NOT NULL DEFAULT 'general';

CREATE INDEX IF NOT EXISTS idx_items_topic
    ON connector_items(topic, created_at DESC);
