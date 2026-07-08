-- 0027: 主动投递闭环 + Digest topic 分桶。
-- 展开式变更。

-- ── 1. scheduled_delivery_requests：send_later 决策产生之持久请求 ──

CREATE TABLE IF NOT EXISTS scheduled_delivery_requests (
    request_id         TEXT PRIMARY KEY,
    principal_id       TEXT NOT NULL DEFAULT 'owner',
    candidate_id       TEXT REFERENCES proactive_candidates(candidate_id),
    content_ref        TEXT,
    suggested_target_json TEXT NOT NULL DEFAULT '{}',
    reason             TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','ready','converted','cancelled','expired')),
    scheduled_at       INTEGER NOT NULL,           -- epoch ms
    expires_at         INTEGER,                    -- epoch ms
    policy_version     INTEGER NOT NULL DEFAULT 1,
    idempotency_key    TEXT NOT NULL UNIQUE,
    created_at         INTEGER NOT NULL,
    converted_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sdr_ready
    ON scheduled_delivery_requests(status, scheduled_at)
    WHERE status = 'pending' AND scheduled_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sdr_candidate
    ON scheduled_delivery_requests(candidate_id);

-- ── 2. digests 加 topic 列（PROACTIVE-IDLE §6 bucket key: principal+date+topic）──

ALTER TABLE digests ADD COLUMN topic TEXT NOT NULL DEFAULT 'general';

CREATE INDEX IF NOT EXISTS idx_digests_date_topic
    ON digests(principal_id, digest_date, topic);

-- ── 3. proactive_tick_log：active Worker tick 实际状态（能量/更新间隔跟踪）──

CREATE TABLE IF NOT EXISTS proactive_ticks (
    tick_id            TEXT PRIMARY KEY,
    principal_id       TEXT NOT NULL DEFAULT 'owner',
    energy_value       REAL NOT NULL DEFAULT 0.0,
    energy_band        TEXT NOT NULL DEFAULT 'medium',
    candidates_evaluated INTEGER NOT NULL DEFAULT 0,
    decisions_json     TEXT NOT NULL DEFAULT '{}',
    dry_run            INTEGER NOT NULL DEFAULT 1,
    next_tick_at       INTEGER,
    created_at         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proactive_ticks_principal
    ON proactive_ticks(principal_id, created_at DESC);
