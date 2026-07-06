-- 0006: Add RunAttempt lease fields and Turn scheduling fields
-- Applied at version 6
--
-- Changes:
-- 1. run_attempts:   add worker_id/lease_owner, lease_version, lease_expires_at (INTEGER),
--                    heartbeat_at (INTEGER), error_ref
-- 2. turns:          add next_attempt_at (INTEGER), completed_at (INTEGER)
--
-- 新字段使用 INTEGER (UTC epoch milliseconds) 适配 DATABASE-SCHEMA / 1。
-- 已有 TEXT 时间列暂不转换，后续 Migration 统一处理。

-- ── run_attempts: lease and reliable fields ──

ALTER TABLE run_attempts ADD COLUMN worker_id            TEXT NOT NULL DEFAULT '';
ALTER TABLE run_attempts ADD COLUMN lease_version        INTEGER NOT NULL DEFAULT 1;
ALTER TABLE run_attempts ADD COLUMN lease_expires_at     INTEGER;
ALTER TABLE run_attempts ADD COLUMN heartbeat_at         INTEGER;
ALTER TABLE run_attempts ADD COLUMN error_ref            TEXT NOT NULL DEFAULT '';

-- ── turns: scheduling fields ──

ALTER TABLE turns ADD COLUMN next_attempt_at  INTEGER;
ALTER TABLE turns ADD COLUMN completed_at     TEXT;
