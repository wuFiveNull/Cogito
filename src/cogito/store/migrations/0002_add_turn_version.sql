-- 0002: Add turn versioning and expand status
-- Applied at version 2
--
-- Changes:
-- 1. Replace turns table with new schema:
--    - Add input_message_id column
--    - Add version column (optimistic concurrency)
--    - Expand status CHECK to accepted/queued/expired
--    - Remove 'created' status

PRAGMA foreign_keys=OFF;

ALTER TABLE turns RENAME TO turns_v1;

CREATE TABLE turns (
    turn_id             TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL DEFAULT '',
    input_message_id    TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted','queued','running','waiting_user','waiting_external','completed','cancelled','failed')),
    priority            INTEGER NOT NULL DEFAULT 80,
    version             INTEGER NOT NULL DEFAULT 1,
    cancel_requested_at TEXT,
    active_attempt_id   TEXT,
    final_message_id    TEXT,
    created_at          TEXT NOT NULL
);

INSERT INTO turns (turn_id, session_id, status, priority, cancel_requested_at, active_attempt_id, final_message_id, created_at)
    SELECT turn_id, session_id,
           CASE WHEN status = 'created' THEN 'accepted' ELSE status END,
           priority, cancel_requested_at, active_attempt_id, final_message_id, created_at
    FROM turns_v1;

DROP TABLE turns_v1;

PRAGMA foreign_keys=ON;
