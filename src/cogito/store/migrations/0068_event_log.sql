-- Canonical append-only event store.  This is additive; contract migration is
-- intentionally separate so running deployments are never dropped in place.

CREATE TABLE IF NOT EXISTS event_log (
    event_id          TEXT PRIMARY KEY,
    stream_type       TEXT NOT NULL,
    stream_id         TEXT NOT NULL,
    stream_version    INTEGER NOT NULL CHECK(stream_version > 0),
    event_type        TEXT NOT NULL,
    type_version      INTEGER NOT NULL DEFAULT 1 CHECK(type_version > 0),
    event_class       TEXT NOT NULL CHECK(event_class IN ('domain','operation','telemetry')),
    producer          TEXT NOT NULL,
    occurred_at       INTEGER NOT NULL,
    trace_id          TEXT NOT NULL DEFAULT '',
    span_id           TEXT NOT NULL DEFAULT '',
    parent_span_id    TEXT,
    correlation_id    TEXT NOT NULL DEFAULT '',
    causation_id      TEXT NOT NULL DEFAULT '',
    actor_id          TEXT NOT NULL DEFAULT '',
    principal_id      TEXT NOT NULL DEFAULT '',
    conversation_id   TEXT NOT NULL DEFAULT '',
    session_id        TEXT NOT NULL DEFAULT '',
    turn_id           TEXT NOT NULL DEFAULT '',
    attempt_id        TEXT NOT NULL DEFAULT '',
    task_id           TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    attributes_json   TEXT NOT NULL DEFAULT '{}',
    payload_ref       TEXT,
    payload_hash      TEXT NOT NULL DEFAULT '',
    outcome           TEXT NOT NULL DEFAULT '',
    error_category    TEXT NOT NULL DEFAULT '',
    idempotency_key   TEXT NOT NULL DEFAULT '',
    UNIQUE(stream_type, stream_id, stream_version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_event_log_idempotency
    ON event_log(producer, idempotency_key)
    WHERE idempotency_key <> '';
CREATE INDEX IF NOT EXISTS idx_event_log_trace
    ON event_log(trace_id, occurred_at, event_id)
    WHERE trace_id <> '';
CREATE INDEX IF NOT EXISTS idx_event_log_session
    ON event_log(session_id, occurred_at, event_id)
    WHERE session_id <> '';
CREATE INDEX IF NOT EXISTS idx_event_log_type_time
    ON event_log(event_type, occurred_at, event_id);
