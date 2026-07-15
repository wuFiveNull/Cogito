-- Durable child-Agent Task -> Turn -> Attempt lifecycle.

ALTER TABLE agent_delegations ADD COLUMN parent_tool_call_id TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_delegations ADD COLUMN join_policy TEXT NOT NULL DEFAULT 'all';
ALTER TABLE agent_delegations ADD COLUMN failure_policy TEXT NOT NULL DEFAULT 'collect';
ALTER TABLE agent_delegations ADD COLUMN result_ref TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_delegations ADD COLUMN child_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_delegations ADD COLUMN completed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_delegations ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_delegations ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_delegations ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_delegations ADD COLUMN cancel_requested_at TEXT;

CREATE TABLE IF NOT EXISTS child_task_links (
    link_id            TEXT PRIMARY KEY,
    delegation_id      TEXT NOT NULL REFERENCES agent_delegations(delegation_id),
    client_id          TEXT NOT NULL,
    task_id            TEXT NOT NULL REFERENCES tasks(task_id),
    turn_id            TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'queued'
                       CHECK(status IN ('queued','running','waiting_user','completed','failed','cancelled')),
    requested_toolsets TEXT NOT NULL DEFAULT '[]',
    result_summary     TEXT NOT NULL DEFAULT '',
    result_ref         TEXT NOT NULL DEFAULT '',
    usage_json         TEXT NOT NULL DEFAULT '{}',
    error              TEXT NOT NULL DEFAULT '',
    version            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    completed_at       TEXT,
    UNIQUE(delegation_id, client_id),
    UNIQUE(task_id)
);

CREATE TABLE IF NOT EXISTS waiting_conditions (
    waiting_id       TEXT PRIMARY KEY,
    owner_type       TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    condition_type   TEXT NOT NULL,
    subject_id       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','satisfied','cancelled','expired')),
    payload_json     TEXT NOT NULL DEFAULT '{}',
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    satisfied_at     TEXT,
    consumed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_child_task_links_delegation
    ON child_task_links(delegation_id, status);
CREATE INDEX IF NOT EXISTS idx_waiting_conditions_owner
    ON waiting_conditions(owner_type, owner_id, status);
