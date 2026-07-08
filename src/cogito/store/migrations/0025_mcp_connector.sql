-- 0025: MCP Connector 支持 —— 把 connector_type 扩展到 mcp，
-- 新增 MCP 映射配置、SourceIngestion 批次日志、MCP 调用统计列。
-- 展开式变更：不破坏现有 RSS/Digest 数据。

-- ── 1. connector_type 扩展：SQLite 只能通过重建表来改 CHECK 约束 ──
-- 由于 connectors 现有数据很少（开发/个人规模），采用事务内重建 + 数据保留。

CREATE TABLE IF NOT EXISTS connectors_v2 (
    connector_id       TEXT PRIMARY KEY,
    connector_type     TEXT NOT NULL DEFAULT 'rss'
                       CHECK(connector_type IN ('rss','json','atom','mcp')),
    name               TEXT NOT NULL DEFAULT '',
    url                TEXT NOT NULL DEFAULT '',
    site_link          TEXT NOT NULL DEFAULT '',
    poll_schedule_id   TEXT REFERENCES schedules(schedule_id),
    fetch_timeout_s    INTEGER NOT NULL DEFAULT 30,
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK(status IN ('active','paused','disabled','error')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at    INTEGER,
    last_attempt_at    INTEGER,
    created_at         INTEGER NOT NULL
);

INSERT OR REPLACE INTO connectors_v2
    (connector_id, connector_type, name, url, site_link, poll_schedule_id,
     fetch_timeout_s, status, consecutive_failures, last_success_at,
     last_attempt_at, created_at)
SELECT connector_id, connector_type, name, url, site_link, poll_schedule_id,
       fetch_timeout_s, status, consecutive_failures, last_success_at,
       last_attempt_at, created_at
FROM connectors;

DROP TABLE IF EXISTS connectors;
ALTER TABLE connectors_v2 RENAME TO connectors;

CREATE INDEX IF NOT EXISTS idx_connectors_status
    ON connectors(status) WHERE status = 'active';

-- ── 2. MCP 映射配置表：每个 MCP 型 Connector 对应一行 ──

CREATE TABLE IF NOT EXISTS mcp_connector_configs (
    connector_id       TEXT PRIMARY KEY REFERENCES connectors(connector_id),
    server_name        TEXT NOT NULL DEFAULT '',
    tool_name          TEXT NOT NULL DEFAULT '',
    arguments_template_json TEXT NOT NULL DEFAULT '{}',
    items_path         TEXT NOT NULL DEFAULT 'items',
    next_cursor_path   TEXT NOT NULL DEFAULT '',
    has_more_path      TEXT NOT NULL DEFAULT 'hasNext',
    stable_id_path     TEXT NOT NULL DEFAULT 'id',
    updated_at_path    TEXT NOT NULL DEFAULT 'publishedAt',
    title_path         TEXT NOT NULL DEFAULT 'title',
    body_path          TEXT NOT NULL DEFAULT 'summary',
    url_path           TEXT NOT NULL DEFAULT 'url',
    topic_path         TEXT NOT NULL DEFAULT 'category',
    max_pages_per_poll INTEGER NOT NULL DEFAULT 5,
    max_items_per_poll INTEGER NOT NULL DEFAULT 200,
    max_output_bytes   INTEGER NOT NULL DEFAULT 1048576,
    config_version     INTEGER NOT NULL DEFAULT 1,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_cfg_server
    ON mcp_connector_configs(server_name, tool_name);

-- ── 3. Source Ingestion 批次日志 ──

CREATE TABLE IF NOT EXISTS ingestion_batches (
    batch_id           TEXT PRIMARY KEY,
    connector_id       TEXT NOT NULL REFERENCES connectors(connector_id),
    task_id            TEXT,
    attempt_id         TEXT,
    status             TEXT NOT NULL DEFAULT 'started'
                       CHECK(status IN ('started','committed','partial','failed','quarantined')),
    cursor_before_json TEXT NOT NULL DEFAULT '{}',
    cursor_after_json  TEXT NOT NULL DEFAULT '{}',
    fetched_count      INTEGER NOT NULL DEFAULT 0,
    accepted_count     INTEGER NOT NULL DEFAULT 0,
    duplicate_count    INTEGER NOT NULL DEFAULT 0,
    quarantined_count  INTEGER NOT NULL DEFAULT 0,
    started_at         INTEGER NOT NULL,
    completed_at       INTEGER,
    error_ref          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ingestion_connector
    ON ingestion_batches(connector_id, started_at DESC);

-- ── 4. connector_items 增加 source_metadata JSON 列（MCP 原始字段） ──

ALTER TABLE connector_items ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}';
