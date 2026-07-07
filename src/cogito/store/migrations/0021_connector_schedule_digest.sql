-- 0021: Connector / Scheduler / Digest —— 数据摄取管道
-- 一个 migration 建立全部新表（表间无破坏性依赖，均带 IF NOT EXISTS）。

-- ── Schedule ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id        TEXT PRIMARY KEY,
    schedule_type      TEXT NOT NULL DEFAULT 'interval'
                       CHECK(schedule_type IN ('once','interval','cron')),
    expression         TEXT NOT NULL DEFAULT '30m',
    timezone           TEXT NOT NULL DEFAULT 'UTC',
    misfire_policy     TEXT NOT NULL DEFAULT 'catch_up_limited'
                       CHECK(misfire_policy IN ('skip','run_once','catch_up_limited','merge')),
    max_catch_up       INTEGER NOT NULL DEFAULT 3,
    enabled            INTEGER NOT NULL DEFAULT 1,
    next_fire_at       INTEGER,   -- epoch ms
    last_fire_at       INTEGER,   -- epoch ms
    version            INTEGER NOT NULL DEFAULT 1,
    connector_id       TEXT,      -- 可选关联 connectors.connector_id
    created_at         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_due
    ON schedules(enabled, next_fire_at)
    WHERE enabled = 1 AND next_fire_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS scheduled_fires (
    fire_id            TEXT PRIMARY KEY,
    schedule_id        TEXT NOT NULL REFERENCES schedules(schedule_id),
    scheduled_fire_at  INTEGER NOT NULL,   -- epoch ms
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','fired','skipped')),
    task_id            TEXT,      -- 关联 tasks.task_id
    created_at         INTEGER NOT NULL,
    UNIQUE(schedule_id, scheduled_fire_at)
);

CREATE INDEX IF NOT EXISTS idx_fires_schedule
    ON scheduled_fires(schedule_id, status);

-- ── Connector ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS connectors (
    connector_id       TEXT PRIMARY KEY,
    connector_type     TEXT NOT NULL DEFAULT 'rss'
                       CHECK(connector_type IN ('rss','json','atom')),
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

CREATE INDEX IF NOT EXISTS idx_connectors_status
    ON connectors(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS connector_cursors (
    connector_id       TEXT PRIMARY KEY REFERENCES connectors(connector_id),
    etag               TEXT NOT NULL DEFAULT '',
    last_modified      TEXT NOT NULL DEFAULT '',
    last_item_ids      TEXT NOT NULL DEFAULT '[]',  -- JSON array
    last_polled_at     INTEGER,
    cursor_json        TEXT NOT NULL DEFAULT '{}',  -- 扩展用
    updated_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_raw_items (
    raw_item_id        TEXT PRIMARY KEY,
    connector_id       TEXT NOT NULL REFERENCES connectors(connector_id),
    source_item_id     TEXT NOT NULL DEFAULT '',
    fetched_at         INTEGER NOT NULL,
    content_hash       TEXT NOT NULL DEFAULT '',
    payload_ref        TEXT,      -- payload_objects.payload_ref
    http_etag          TEXT NOT NULL DEFAULT '',
    http_last_modified TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_raw_connector_hash
    ON connector_raw_items(connector_id, content_hash);

CREATE TABLE IF NOT EXISTS connector_items (
    item_id            TEXT PRIMARY KEY,
    connector_id       TEXT NOT NULL REFERENCES connectors(connector_id),
    raw_item_id        TEXT REFERENCES connector_raw_items(raw_item_id),
    source_item_id     TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    link               TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    author             TEXT NOT NULL DEFAULT '',
    published_at       INTEGER,
    content_hash       TEXT NOT NULL DEFAULT '',
    relevance          REAL,
    summary_text       TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'new'
                       CHECK(status IN ('new','silent','digest','sent','duplicate','ignored')),
    created_at         INTEGER NOT NULL,
    UNIQUE(connector_id, source_item_id)
);

CREATE INDEX IF NOT EXISTS idx_items_connector_status
    ON connector_items(connector_id, status);
CREATE INDEX IF NOT EXISTS idx_items_hash
    ON connector_items(connector_id, content_hash);

-- ── Proactive Decision / Digest ────────────────────────────

CREATE TABLE IF NOT EXISTS proactive_decisions (
    decision_id        TEXT PRIMARY KEY,
    item_id            TEXT NOT NULL REFERENCES connector_items(item_id),
    decision           TEXT NOT NULL
                       CHECK(decision IN ('digest','silent','send_now','ignore')),
    relevance_score    REAL,
    reason             TEXT NOT NULL DEFAULT '',
    decided_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    digest_id          TEXT PRIMARY KEY,
    principal_id       TEXT NOT NULL DEFAULT '',
    digest_date        TEXT NOT NULL,     -- YYYY-MM-DD
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','ready','sent','expired')),
    item_count         INTEGER NOT NULL DEFAULT 0,
    content_ref        TEXT,              -- payload_objects.payload_ref（渲染后）
    created_at         INTEGER NOT NULL,
    rendered_at        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_digests_date
    ON digests(principal_id, digest_date);

CREATE TABLE IF NOT EXISTS digest_items (
    digest_id          TEXT NOT NULL REFERENCES digests(digest_id),
    item_id            TEXT NOT NULL REFERENCES connector_items(item_id),
    PRIMARY KEY (digest_id, item_id)
);
