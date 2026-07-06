-- 0005: Add reliability fields for outbox, delivery lease and retry
-- Applied at version 5
--
-- Changes:
-- 1. outbox_events: add attempt_count, next_attempt_at, lease_version, safe_error
-- 2. deliveries:    add attempt_count, next_attempt_at, lease_owner, lease_expires_at, lease_version
-- 3. delivery_attempts: add lease_owner, lease_version

-- ── outbox_events: reliability fields ──

ALTER TABLE outbox_events ADD COLUMN attempt_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox_events ADD COLUMN next_attempt_at   TEXT;
ALTER TABLE outbox_events ADD COLUMN lease_version     INTEGER NOT NULL DEFAULT 1;
ALTER TABLE outbox_events ADD COLUMN safe_error        TEXT NOT NULL DEFAULT '';

-- ── deliveries: lease and reliability fields ──

ALTER TABLE deliveries ADD COLUMN attempt_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deliveries ADD COLUMN next_attempt_at   TEXT;
ALTER TABLE deliveries ADD COLUMN lease_owner       TEXT;
ALTER TABLE deliveries ADD COLUMN lease_expires_at  TEXT;
ALTER TABLE deliveries ADD COLUMN lease_version     INTEGER NOT NULL DEFAULT 1;

-- ── delivery_attempts: lease fields ──

ALTER TABLE delivery_attempts ADD COLUMN lease_owner    TEXT NOT NULL DEFAULT '';
ALTER TABLE delivery_attempts ADD COLUMN lease_version  INTEGER NOT NULL DEFAULT 1;
