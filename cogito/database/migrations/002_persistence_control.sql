-- Migration v1 → v2: PersistencePhase control tables
--
-- Adds 4 new control tables needed by the PersistencePhase transaction
-- pipeline, plus ALTER TABLE on existing tables for turn/request tracking.
--
-- Design: see Cogito-Agent_PersistencePhase_最终实现规范 §4

-- ============================================================
-- 1. Sessions — sequential tracking per conversation
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    session_id              TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,

    version                 INTEGER NOT NULL DEFAULT 0
                            CHECK (version >= 0),

    next_seq_no             INTEGER NOT NULL DEFAULT 1
                            CHECK (next_seq_no >= 1),

    summary_text            TEXT,
    summary_version         INTEGER NOT NULL DEFAULT 0
                            CHECK (summary_version >= 0),
    summary_updated_at      TEXT,

    last_turn_id            TEXT,
    last_request_id         TEXT,
    last_message_at         TEXT,

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;

CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
ON sessions(user_id, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_sessions_touch_updated_at
AFTER UPDATE ON sessions
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE sessions
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE session_id = NEW.session_id;
END;

-- ============================================================
-- 2. Turn commits — idempotency tracking
-- ============================================================
CREATE TABLE IF NOT EXISTS turn_commits (
    commit_id               TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    session_id              TEXT NOT NULL
                            REFERENCES sessions(session_id),
    request_id              TEXT NOT NULL,
    turn_id                 TEXT NOT NULL,

    commit_fingerprint      TEXT NOT NULL,

    user_event_id           TEXT NOT NULL
                            REFERENCES events(id),
    assistant_event_id      TEXT NOT NULL
                            REFERENCES events(id),

    session_version         INTEGER NOT NULL
                            CHECK (session_version >= 1),

    outcome_json            TEXT NOT NULL
                            CHECK (
                                json_valid(outcome_json)
                                AND json_type(outcome_json) = 'object'
                            ),

    persistence_span_id     TEXT REFERENCES trace_events(id),

    committed_at            TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),

    UNIQUE(user_id, request_id),
    UNIQUE(turn_id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_turn_commits_session
ON turn_commits(session_id, committed_at DESC);

-- ============================================================
-- 3. Candidate write audits — every candidate gets a verdict
-- ============================================================
CREATE TABLE IF NOT EXISTS candidate_write_audits (
    id                      TEXT PRIMARY KEY,
    commit_id               TEXT NOT NULL
                            REFERENCES turn_commits(commit_id)
                            ON DELETE CASCADE,
    user_id                 TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    turn_id                 TEXT NOT NULL,

    candidate_id            TEXT NOT NULL,
    candidate_type          TEXT NOT NULL
                            CHECK (
                                candidate_type IN (
                                    'preference',
                                    'memory',
                                    'summary'
                                )
                            ),
    candidate_key           TEXT,
    requested_operation     TEXT NOT NULL,
    result_status           TEXT NOT NULL
                            CHECK (
                                result_status IN (
                                    'applied_insert',
                                    'applied_update',
                                    'applied_delete',
                                    'superseded',
                                    'deduplicated',
                                    'tentative',
                                    'ignored',
                                    'rejected'
                                )
                            ),
    target_record_id        TEXT,
    reason_code             TEXT,
    confidence              REAL,
    importance              REAL,
    source_event_ids_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(source_event_ids_json)
                                AND json_type(source_event_ids_json) = 'array'
                            ),
    metadata_json           TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(metadata_json)),
    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),

    UNIQUE(commit_id, candidate_id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_candidate_audits_turn
ON candidate_write_audits(turn_id, candidate_type);

-- ============================================================
-- 4. Embedding jobs — async embedding compensation
-- ============================================================
CREATE TABLE IF NOT EXISTS embedding_jobs (
    id                      TEXT PRIMARY KEY,
    memory_id               TEXT NOT NULL
                            REFERENCES memories(id)
                            ON DELETE CASCADE,
    embedding_model         TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (
                                status IN (
                                    'pending',
                                    'processing',
                                    'done',
                                    'failed'
                                )
                            ),
    attempts                INTEGER NOT NULL DEFAULT 0
                            CHECK (attempts >= 0),
    last_error              TEXT,
    available_at            TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),

    UNIQUE(memory_id, embedding_model)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_embedding_jobs_pending
ON embedding_jobs(status, available_at)
WHERE status IN ('pending', 'failed');

-- ============================================================
-- 5. ALTER existing events — add turn/request association
-- ============================================================
ALTER TABLE events ADD COLUMN request_id TEXT;
ALTER TABLE events ADD COLUMN turn_id TEXT;

CREATE INDEX IF NOT EXISTS idx_events_request
ON events(user_id, request_id)
WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_events_turn_seq
ON events(turn_id, seq_no)
WHERE turn_id IS NOT NULL;

-- ============================================================
-- 6. ALTER existing trace_events — add turn/request association
-- ============================================================
ALTER TABLE trace_events ADD COLUMN request_id TEXT;
ALTER TABLE trace_events ADD COLUMN turn_id TEXT;

CREATE INDEX IF NOT EXISTS idx_trace_events_request
ON trace_events(user_id, request_id, started_at)
WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trace_events_turn
ON trace_events(turn_id, started_at)
WHERE turn_id IS NOT NULL;

-- ============================================================
-- 7. Schema version
-- ============================================================
PRAGMA user_version = 2;
