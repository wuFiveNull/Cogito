-- 0061: persistent process, delegation, and generic Agent schedule metadata.

ALTER TABLE schedules ADD COLUMN task_type TEXT NOT NULL DEFAULT 'connector.poll';
ALTER TABLE schedules ADD COLUMN task_payload TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS tool_processes (
    process_id       TEXT PRIMARY KEY,
    attempt_id       TEXT NOT NULL DEFAULT '',
    container_id     TEXT NOT NULL DEFAULT '',
    command_summary  TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK(status IN ('running','completed','failed','cancelled','unknown')),
    exit_code        INTEGER,
    output_ref       TEXT,
    created_at       TEXT NOT NULL,
    completed_at     TEXT
);

CREATE TABLE IF NOT EXISTS agent_delegations (
    delegation_id     TEXT PRIMARY KEY,
    parent_turn_id    TEXT NOT NULL DEFAULT '',
    parent_attempt_id TEXT NOT NULL DEFAULT '',
    principal_id      TEXT NOT NULL DEFAULT '',
    depth             INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK(status IN ('queued','running','cancel_requested','completed','failed','cancelled')),
    budget_json       TEXT NOT NULL DEFAULT '{}',
    prompt            TEXT NOT NULL DEFAULT '',
    result_text       TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    completed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_processes_attempt
    ON tool_processes(attempt_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_delegations_parent
    ON agent_delegations(parent_turn_id, status);
