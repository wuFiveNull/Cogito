-- 0007: Convert time columns to UTC epoch milliseconds INTEGER
-- Applied at version 7
--
-- 按照 DATABASE-SCHEMA / 1. SQLite 模式，数据库时间统一保存为 INTEGER。
-- 覆盖当前执行链使用的表：turns, run_attempts, outbox_events, deliveries, delivery_attempts。
--
-- 对于旧 TEXT 时间（ISO 8601 格式），通过 unixepoch() 转换为 epoch ms。
-- 对于新 INTEGER 时间（0006 已使用 INT），直接保留。
-- 对于整数格式的 TEXT（如 "1736942520000"），直接 CAST 为 INTEGER。
--
-- 兼容升级：v5→v7, v6→v7。

PRAGMA foreign_keys=OFF;

-- =============================================================================
-- 1. turns
-- =============================================================================
ALTER TABLE turns RENAME TO turns_v7;

CREATE TABLE turns (
    turn_id             TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL DEFAULT '',
    input_message_id    TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted','queued','running','waiting_user','waiting_external','completed','cancelled','failed')),
    priority            INTEGER NOT NULL DEFAULT 80,
    version             INTEGER NOT NULL DEFAULT 1,
    cancel_requested_at INTEGER,
    active_attempt_id   TEXT,
    final_message_id    TEXT,
    created_at          INTEGER NOT NULL,
    next_attempt_at     INTEGER,
    completed_at        INTEGER
);

INSERT INTO turns (
    turn_id, session_id, input_message_id, status, priority, version,
    cancel_requested_at, active_attempt_id, final_message_id, created_at,
    next_attempt_at, completed_at
)
SELECT
    turn_id, session_id, input_message_id, status, priority, version,
    COALESCE(CAST(unixepoch(cancel_requested_at) AS INTEGER) * 1000, CAST(cancel_requested_at AS INTEGER)),
    active_attempt_id, final_message_id,
    COALESCE(CAST(unixepoch(created_at) AS INTEGER) * 1000, CAST(created_at AS INTEGER)),
    next_attempt_at,
    COALESCE(CAST(unixepoch(completed_at) AS INTEGER) * 1000, CAST(completed_at AS INTEGER))
FROM turns_v7;

DROP TABLE turns_v7;

-- =============================================================================
-- 2. run_attempts
-- =============================================================================
ALTER TABLE run_attempts RENAME TO run_attempts_v7;

CREATE TABLE run_attempts (
    attempt_id      TEXT PRIMARY KEY,
    turn_id         TEXT NOT NULL REFERENCES turns(turn_id),
    attempt_no      INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','running','succeeded','failed','cancelled','abandoned')),
    checkpoint_ref  TEXT,
    started_at      INTEGER,
    finished_at     INTEGER,
    worker_id       TEXT NOT NULL DEFAULT '',
    lease_version   INTEGER NOT NULL DEFAULT 1,
    lease_expires_at INTEGER,
    heartbeat_at    INTEGER,
    error_ref       TEXT NOT NULL DEFAULT '',
    UNIQUE(turn_id, attempt_no)
);

INSERT INTO run_attempts (
    attempt_id, turn_id, attempt_no, status, checkpoint_ref,
    started_at, finished_at, worker_id, lease_version,
    lease_expires_at, heartbeat_at, error_ref
)
SELECT
    attempt_id, turn_id, attempt_no, status, checkpoint_ref,
    COALESCE(CAST(unixepoch(started_at) AS INTEGER) * 1000, CAST(started_at AS INTEGER)),
    COALESCE(CAST(unixepoch(finished_at) AS INTEGER) * 1000, CAST(finished_at AS INTEGER)),
    worker_id, lease_version,
    lease_expires_at,
    heartbeat_at,
    error_ref
FROM run_attempts_v7;

DROP TABLE run_attempts_v7;

-- =============================================================================
-- 3. outbox_events
-- =============================================================================
ALTER TABLE outbox_events RENAME TO outbox_events_v7;

CREATE TABLE outbox_events (
    event_id          TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL DEFAULT '',
    aggregate_type    TEXT NOT NULL DEFAULT '',
    aggregate_id      TEXT NOT NULL DEFAULT '',
    aggregate_version INTEGER NOT NULL DEFAULT 1,
    payload_ref       TEXT,
    content_hash      TEXT NOT NULL DEFAULT '',
    schema_version    TEXT NOT NULL DEFAULT '1.0',
    correlation_id    TEXT NOT NULL DEFAULT '',
    causation_id      TEXT NOT NULL DEFAULT '',
    origin            TEXT NOT NULL DEFAULT 'system',
    trust_label       TEXT NOT NULL DEFAULT 'unverified',
    status            TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','leased','published','retry_scheduled','dead_letter')),
    lease_owner       TEXT,
    lease_expires_at  INTEGER,
    created_at        INTEGER NOT NULL,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   INTEGER,
    lease_version     INTEGER NOT NULL DEFAULT 1,
    safe_error        TEXT NOT NULL DEFAULT ''
);

INSERT INTO outbox_events (
    event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
    payload_ref, content_hash, schema_version, correlation_id, causation_id,
    origin, trust_label, status, lease_owner, lease_expires_at, created_at,
    attempt_count, next_attempt_at, lease_version, safe_error
)
SELECT
    event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
    payload_ref, content_hash, schema_version, correlation_id, causation_id,
    origin, trust_label, status, lease_owner,
    COALESCE(CAST(unixepoch(lease_expires_at) AS INTEGER) * 1000, CAST(lease_expires_at AS INTEGER)),
    COALESCE(CAST(unixepoch(created_at) AS INTEGER) * 1000, CAST(created_at AS INTEGER)),
    attempt_count,
    COALESCE(CAST(unixepoch(next_attempt_at) AS INTEGER) * 1000, CAST(next_attempt_at AS INTEGER)),
    lease_version, safe_error
FROM outbox_events_v7;

DROP TABLE outbox_events_v7;

CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status, created_at);
CREATE INDEX IF NOT EXISTS idx_outbox_aggregate ON outbox_events(aggregate_type, aggregate_id, aggregate_version);

-- =============================================================================
-- 4. deliveries
-- =============================================================================
ALTER TABLE deliveries RENAME TO deliveries_v7;

CREATE TABLE deliveries (
    delivery_id        TEXT PRIMARY KEY,
    target_snapshot    TEXT NOT NULL DEFAULT '{}',
    content_ref        TEXT,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','scheduled','sending','sent','partially_sent','streaming','finalizing','interrupted','unknown','retry_scheduled','failed','cancelled')),
    idempotency_key    TEXT NOT NULL DEFAULT '',
    scheduled_at       INTEGER,
    platform_message_id TEXT,
    last_error         TEXT,
    created_at         INTEGER NOT NULL,
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    next_attempt_at    INTEGER,
    lease_owner        TEXT,
    lease_expires_at   INTEGER,
    lease_version      INTEGER NOT NULL DEFAULT 1
);

INSERT INTO deliveries (
    delivery_id, target_snapshot, content_ref, status, idempotency_key,
    scheduled_at, platform_message_id, last_error, created_at,
    attempt_count, next_attempt_at, lease_owner, lease_expires_at, lease_version
)
SELECT
    delivery_id, target_snapshot, content_ref, status, idempotency_key,
    COALESCE(CAST(unixepoch(scheduled_at) AS INTEGER) * 1000, CAST(scheduled_at AS INTEGER)),
    platform_message_id, last_error,
    COALESCE(CAST(unixepoch(created_at) AS INTEGER) * 1000, CAST(created_at AS INTEGER)),
    attempt_count,
    COALESCE(CAST(unixepoch(next_attempt_at) AS INTEGER) * 1000, CAST(next_attempt_at AS INTEGER)),
    lease_owner,
    COALESCE(CAST(unixepoch(lease_expires_at) AS INTEGER) * 1000, CAST(lease_expires_at AS INTEGER)),
    lease_version
FROM deliveries_v7;

DROP TABLE deliveries_v7;

CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status, scheduled_at);

-- =============================================================================
-- 5. delivery_attempts
-- =============================================================================
ALTER TABLE delivery_attempts RENAME TO delivery_attempts_v7;

CREATE TABLE delivery_attempts (
    attempt_id       TEXT PRIMARY KEY,
    delivery_id      TEXT NOT NULL REFERENCES deliveries(delivery_id),
    attempt_no       INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','sending','succeeded','failed')),
    started_at       INTEGER,
    finished_at      INTEGER,
    platform_receipt TEXT NOT NULL DEFAULT '{}',
    error            TEXT,
    lease_owner      TEXT NOT NULL DEFAULT '',
    lease_version    INTEGER NOT NULL DEFAULT 1,
    UNIQUE(delivery_id, attempt_no)
);

INSERT INTO delivery_attempts (
    attempt_id, delivery_id, attempt_no, status,
    started_at, finished_at, platform_receipt, error,
    lease_owner, lease_version
)
SELECT
    attempt_id, delivery_id, attempt_no, status,
    COALESCE(CAST(unixepoch(started_at) AS INTEGER) * 1000, CAST(started_at AS INTEGER)),
    COALESCE(CAST(unixepoch(finished_at) AS INTEGER) * 1000, CAST(finished_at AS INTEGER)),
    platform_receipt, error,
    lease_owner, lease_version
FROM delivery_attempts_v7;

DROP TABLE delivery_attempts_v7;

PRAGMA foreign_keys=ON;
