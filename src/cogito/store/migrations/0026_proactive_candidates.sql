-- 0026: Proactive 候选 + Consumer Inbox + ProactivePolicy。
-- M5/M6 共用 migration；展开式变更，不影响已有 connector_items/outbox 数据。

-- ── 1. event_consumptions：Consumer 幂等防重复 ──

CREATE TABLE IF NOT EXISTS event_consumptions (
    consumer_name      TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','succeeded','failed','skipped')),
    attempts           INTEGER NOT NULL DEFAULT 0,
    processed_at       INTEGER,
    error              TEXT NOT NULL DEFAULT '',
    -- 复合唯一键 TASKS/SCHEMA/(consumer_name, event_id)
    PRIMARY KEY (consumer_name, event_id)
);

CREATE INDEX IF NOT EXISTS idx_consumptions_status
    ON event_consumptions(status);

-- ── 2. proactive_candidates：主动候选（projection 层）──

CREATE TABLE IF NOT EXISTS proactive_candidates (
    candidate_id       TEXT PRIMARY KEY,
    principal_id       TEXT NOT NULL DEFAULT 'owner',
    stream_type        TEXT NOT NULL DEFAULT 'content'
                       CHECK(stream_type IN ('alert','content','context')),
    topic              TEXT NOT NULL DEFAULT 'general',
    summary            TEXT NOT NULL DEFAULT '',
    novelty            REAL NOT NULL DEFAULT 0.5,
    relevance          REAL NOT NULL DEFAULT 0.0,
    urgency            REAL NOT NULL DEFAULT 0.0,
    confidence         REAL NOT NULL DEFAULT 0.5,
    recommended_action TEXT NOT NULL DEFAULT 'evaluate'
                       CHECK(recommended_action IN (
                           'evaluate','send_now','send_later','digest',
                           'silent','discard','ask_permission','create_task')),
    policy_version     INTEGER NOT NULL DEFAULT 1,
    idempotency_key    TEXT NOT NULL UNIQUE,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    source_payload_ref TEXT,
    expires_at_value   INTEGER,            -- epoch ms；由 Policy 默认 TTL 决定
    created_at         INTEGER NOT NULL,
    consumed_at        INTEGER,
    status             TEXT NOT NULL DEFAULT 'evaluating'
                       CHECK(status IN (
                           'evaluating','queued','decided','consumed','expired','quarantined'))
);

CREATE INDEX IF NOT EXISTS idx_candidates_principal_status
    ON proactive_candidates(principal_id, status, stream_type);
CREATE INDEX IF NOT EXISTS idx_candidates_idempotency
    ON proactive_candidates(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_candidates_created
    ON proactive_candidates(principal_id, created_at DESC);

-- ── 3. proactive_policies：版化化、可调优 ──

CREATE TABLE IF NOT EXISTS proactive_policies (
    policy_id          TEXT PRIMARY KEY,
    principal_id       TEXT NOT NULL DEFAULT 'owner',
    version            INTEGER NOT NULL DEFAULT 1,
    allow_topics_json  TEXT NOT NULL DEFAULT '[]',
    deny_topics_json   TEXT NOT NULL DEFAULT '[]',
    quiet_hours_json   TEXT NOT NULL DEFAULT '{"enabled":true,"start":"23:00","end":"08:00","timezone":"Asia/Shanghai"}',
    cooldown_json      TEXT NOT NULL DEFAULT '{"same_topic_minutes":360}',
    budgets_json       TEXT NOT NULL DEFAULT '{"max_pushes_per_hour":3,"max_pushes_per_day":10}',
    dry_run            INTEGER NOT NULL DEFAULT 1,
    filters_json       TEXT NOT NULL DEFAULT '{}',
    updated_by         TEXT,
    updated_at         INTEGER NOT NULL,
    UNIQUE(principal_id, version)
);

CREATE INDEX IF NOT EXISTS idx_policies_principal_ver
    ON proactive_policies(principal_id, version DESC);

-- ── 4. proactive_decisions_v2：v1 仅引用 item_id；v2 引用 candidate_id ──

CREATE TABLE IF NOT EXISTS proactive_decisions_v2 (
    decision_id        TEXT PRIMARY KEY,
    candidate_id       TEXT NOT NULL REFERENCES proactive_candidates(candidate_id),
    principal_id       TEXT NOT NULL DEFAULT 'owner',
    action             TEXT NOT NULL
                       CHECK(action IN (
                           'send_now','send_later','digest','silent',
                           'discard','ask_permission','create_task')),
    rule_results_json  TEXT NOT NULL DEFAULT '{}',
    model_score_json   TEXT,
    policy_version     INTEGER NOT NULL DEFAULT 1,
    energy_value       REAL,
    dry_run            INTEGER NOT NULL DEFAULT 1,
    decided_at         INTEGER NOT NULL,
    scheduled_for      INTEGER,
    delivery_id        TEXT,
    digest_id          TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_candidate
    ON proactive_decisions_v2(candidate_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_principal
    ON proactive_decisions_v2(principal_id, dry_run);

-- ── 5. connector_items 加 topic 列（M5 projection 用；M4 已加 source_metadata_json）──

ALTER TABLE connector_items ADD COLUMN topic_json TEXT NOT NULL DEFAULT '{}';
