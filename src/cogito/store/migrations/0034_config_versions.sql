-- 0034: config_versions —— 配置版本审计（Plan 06 M2）。
-- 每次启动/热更新插入一条，Attempt/Task/Decision 可追溯使用的 config hash。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS config_versions (
    version_id          TEXT PRIMARY KEY,
    content_hash        TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    source_layers       TEXT NOT NULL DEFAULT '[]',
    applied_at          INTEGER NOT NULL,
    applied_by          TEXT,
    change_summary      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_config_versions_hash
    ON config_versions(content_hash);

CREATE INDEX IF NOT EXISTS idx_config_versions_applied
    ON config_versions(applied_at DESC);
