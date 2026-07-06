-- 0004: Fix FK references in run_attempts and turn_checkpoints
-- Applied at version 4
--
-- SQLite's ALTER TABLE RENAME rewrites FK references in existing tables.
-- If migration 0002 (v1) was applied, run_attempts and turn_checkpoints
-- may still reference "turns_v1" instead of "turns".
-- This migration recreates those tables with correct FK references.

PRAGMA foreign_keys=OFF;

-- Fix run_attempts FK if it references turns_v1
ALTER TABLE run_attempts RENAME TO run_attempts_v4;
CREATE TABLE run_attempts (
    attempt_id      TEXT PRIMARY KEY,
    turn_id         TEXT NOT NULL REFERENCES turns(turn_id),
    attempt_no      INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','running','succeeded','failed','cancelled','abandoned')),
    checkpoint_ref  TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    UNIQUE(turn_id, attempt_no)
);
INSERT INTO run_attempts SELECT * FROM run_attempts_v4;
DROP TABLE run_attempts_v4;

-- Fix turn_checkpoints FK if it references turns_v1
ALTER TABLE turn_checkpoints RENAME TO turn_checkpoints_v4;
CREATE TABLE turn_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    turn_id       TEXT NOT NULL REFERENCES turns(turn_id),
    data          TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
INSERT INTO turn_checkpoints SELECT * FROM turn_checkpoints_v4;
DROP TABLE turn_checkpoints_v4;

PRAGMA foreign_keys=ON;
