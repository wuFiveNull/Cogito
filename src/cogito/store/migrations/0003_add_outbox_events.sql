-- 0003: Add event outbox, events, message_revisions tables
-- Applied at version 3
--
-- Changes:
-- 1. outbox_events — transactional outbox for domain events
-- 2. events — event sourcing table (append-only event log)
-- 3. message_revisions — platform edit tracking
-- 4. messages.deleted_at — soft delete support

-- ── Event Outbox ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS outbox_events (
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
    lease_expires_at  TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status, created_at);
CREATE INDEX IF NOT EXISTS idx_outbox_aggregate ON outbox_events(aggregate_type, aggregate_id, aggregate_version);

-- ── Events (Append-Only Event Log) ────────────────────────

CREATE TABLE IF NOT EXISTS events (
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
    occurred_at       TEXT NOT NULL,
    UNIQUE(aggregate_type, aggregate_id, aggregate_version)
);

CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_type, aggregate_id, aggregate_version);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, occurred_at);

-- ── Message Revisions ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS message_revisions (
    message_id          TEXT NOT NULL REFERENCES messages(message_id),
    revision_no         INTEGER NOT NULL,
    platform_edit_id    TEXT NOT NULL DEFAULT '',
    platform_revision   INTEGER NOT NULL DEFAULT 0,
    edited_at           TEXT,
    observed_at         TEXT,
    editor_endpoint_id  TEXT NOT NULL DEFAULT '',
    content_parts       TEXT NOT NULL DEFAULT '[]',
    raw_payload_ref     TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (message_id, revision_no),
    UNIQUE(message_id, platform_edit_id)
);

-- ── messages: add deleted_at for soft delete ──────────────

ALTER TABLE messages ADD COLUMN deleted_at TEXT;
