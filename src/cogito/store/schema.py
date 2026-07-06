"""SQLite schema — all CREATE TABLE statements."""

SCHEMA_SQL = """

-- Schema version tracking
CREATE TABLE IF NOT EXISTS _schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    checksum    TEXT    NOT NULL DEFAULT ''
);

-- ── Identity ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS principals (
    principal_id   TEXT PRIMARY KEY,
    principal_type TEXT NOT NULL DEFAULT 'owner' CHECK(principal_type IN ('owner','external_user','system')),
    status         TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','blocked','deleted')),
    created_at     TEXT NOT NULL,
    metadata       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS endpoints (
    endpoint_id         TEXT PRIMARY KEY,
    channel_type        TEXT NOT NULL DEFAULT '',
    channel_instance_id TEXT NOT NULL DEFAULT '',
    platform_account_id TEXT NOT NULL DEFAULT '',
    principal_id        TEXT NOT NULL REFERENCES principals(principal_id),
    endpoint_ref        TEXT NOT NULL DEFAULT '',
    capabilities        TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','deleted')),
    verified_at         TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id            TEXT PRIMARY KEY,
    conversation_endpoint_id   TEXT NOT NULL DEFAULT '',
    platform_conversation_id   TEXT NOT NULL DEFAULT '',
    conversation_endpoint_ref  TEXT NOT NULL DEFAULT '',
    conversation_type          TEXT NOT NULL DEFAULT 'private' CHECK(conversation_type IN ('private','group','thread','web')),
    principal_scope            TEXT NOT NULL DEFAULT '',
    context_partition_policy   TEXT NOT NULL DEFAULT 'isolated' CHECK(context_partition_policy IN ('isolated','shared_profile')),
    status                     TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted'))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id             TEXT PRIMARY KEY,
    conversation_id        TEXT NOT NULL REFERENCES conversations(conversation_id),
    context_partition_key  TEXT NOT NULL DEFAULT '',
    reset_generation       INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','expired','closed')),
    created_at             TEXT NOT NULL,
    UNIQUE(conversation_id, context_partition_key, reset_generation)
);

-- ── Message ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS messages (
    message_id           TEXT PRIMARY KEY,
    conversation_id      TEXT NOT NULL REFERENCES conversations(conversation_id),
    session_id           TEXT NOT NULL DEFAULT '',
    sender_principal_id  TEXT NOT NULL DEFAULT '',
    sender_endpoint_id   TEXT NOT NULL DEFAULT '',
    role                 TEXT NOT NULL CHECK(role IN ('user','assistant','tool','system')),
    direction            TEXT NOT NULL DEFAULT 'inbound' CHECK(direction IN ('inbound','outbound','internal')),
    reply_to_message_id  TEXT,
    platform_message_id  TEXT,
    current_revision_no  INTEGER NOT NULL DEFAULT 1,
    receive_sequence     INTEGER NOT NULL DEFAULT 0,
    trust_label          TEXT NOT NULL DEFAULT 'unverified',
    raw_payload_ref      TEXT,
    reply_route_json     TEXT NOT NULL DEFAULT '{}',
    capability_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT NOT NULL,
    UNIQUE(conversation_id, receive_sequence)
);

CREATE TABLE IF NOT EXISTS content_parts (
    part_id      TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL REFERENCES messages(message_id),
    content_type TEXT NOT NULL DEFAULT 'text',
    inline_data  TEXT NOT NULL DEFAULT '',
    payload_ref  TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL DEFAULT '',
    metadata     TEXT NOT NULL DEFAULT '{}',
    trust_label  TEXT NOT NULL DEFAULT 'unverified'
);

CREATE TABLE IF NOT EXISTS inbound_inbox (
    channel_instance_id TEXT NOT NULL,
    platform_event_id   TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'received' CHECK(status IN ('received','processed','failed')),
    message_id          TEXT REFERENCES messages(message_id),
    received_at         TEXT NOT NULL,
    PRIMARY KEY (channel_instance_id, platform_event_id)
);

-- ── Execution ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS turns (
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

CREATE TABLE IF NOT EXISTS run_attempts (
    attempt_id      TEXT PRIMARY KEY,
    turn_id         TEXT NOT NULL REFERENCES turns(turn_id),
    attempt_no      INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','running','succeeded','failed','cancelled','abandoned')),
    checkpoint_ref  TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    UNIQUE(turn_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS turn_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    turn_id       TEXT NOT NULL REFERENCES turns(turn_id),
    data          TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

-- ── Task ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    task_type        TEXT NOT NULL DEFAULT '',
    payload_ref      TEXT,
    status           TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','scheduled','queued','running','waiting_user','waiting_external','retry_scheduled','completed','failed','cancelled','expired')),
    priority         INTEGER NOT NULL DEFAULT 40,
    scheduled_at     TEXT,
    retry_policy     TEXT NOT NULL DEFAULT '{}',
    lease_owner      TEXT,
    lease_expires_at TEXT,
    checkpoint_ref   TEXT,
    idempotency_key  TEXT NOT NULL DEFAULT '',
    origin           TEXT NOT NULL DEFAULT 'system',
    created_at       TEXT NOT NULL,
    UNIQUE(task_type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS task_attempts (
    task_attempt_id  TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_no       INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','running','succeeded','failed','cancelled','abandoned')),
    lease_owner      TEXT NOT NULL DEFAULT '',
    lease_version    INTEGER NOT NULL DEFAULT 1,
    lease_expires_at TEXT,
    checkpoint_ref   TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    UNIQUE(task_id, attempt_no)
);

-- ── Delivery ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id        TEXT PRIMARY KEY,
    target_snapshot    TEXT NOT NULL DEFAULT '{}',
    content_ref        TEXT,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','scheduled','sending','sent','partially_sent','streaming','finalizing','interrupted','unknown','retry_scheduled','failed','cancelled')),
    idempotency_key    TEXT NOT NULL DEFAULT '',
    scheduled_at       TEXT,
    platform_message_id TEXT,
    last_error         TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id       TEXT PRIMARY KEY,
    delivery_id      TEXT NOT NULL REFERENCES deliveries(delivery_id),
    attempt_no       INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','sending','succeeded','failed')),
    started_at       TEXT,
    finished_at      TEXT,
    platform_receipt TEXT NOT NULL DEFAULT '{}',
    error            TEXT,
    UNIQUE(delivery_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    receipt_id          TEXT PRIMARY KEY,
    delivery_id         TEXT NOT NULL REFERENCES deliveries(delivery_id),
    delivery_attempt_id TEXT NOT NULL DEFAULT '',
    operation_seq       INTEGER NOT NULL DEFAULT 1,
    request_hash        TEXT NOT NULL DEFAULT '',
    receipt_kind        TEXT NOT NULL DEFAULT 'uncertain'
                        CHECK(receipt_kind IN ('confirmed', 'uncertain', 'reconciled')),
    platform_message_id TEXT,
    safe_result         TEXT,
    observed_at         INTEGER NOT NULL,
    lease_version       INTEGER NOT NULL DEFAULT 1,
    UNIQUE(delivery_id, delivery_attempt_id, operation_seq)
);

-- ── Capability ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id    TEXT PRIMARY KEY,
    attempt_id      TEXT NOT NULL,
    attempt_type    TEXT NOT NULL CHECK(attempt_type IN ('run','task')),
    tool_name       TEXT NOT NULL DEFAULT '',
    tool_version    TEXT NOT NULL DEFAULT '1.0',
    arguments       TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','executing','succeeded','failed','unknown','cancelled')),
    started_at      TEXT,
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    turn_id         TEXT,
    request         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','expired','cancelled')),
    responder_id    TEXT,
    decided_at      TEXT,
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- ── Cognition ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_items (
    memory_id       TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK(kind IN ('fact','preference','episode','goal','constraint')),
    subject         TEXT NOT NULL DEFAULT '',
    predicate       TEXT NOT NULL DEFAULT '',
    value           TEXT NOT NULL DEFAULT '',
    scope           TEXT NOT NULL DEFAULT '',
    source_type     TEXT NOT NULL DEFAULT '',
    source_id       TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 1.0,
    status          TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','confirmed','rejected','expired')),
    valid_from      TEXT,
    valid_to        TEXT,
    supersedes_id   TEXT,
    goal_status     TEXT CHECK(goal_status IN ('active','paused','completed','cancelled','expired')),
    goal_priority   INTEGER,
    goal_deadline   TEXT,
    goal_progress   REAL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    model_call_id       TEXT PRIMARY KEY,
    attempt_id          TEXT NOT NULL DEFAULT '',
    request_id          TEXT NOT NULL DEFAULT '',
    provider_id         TEXT NOT NULL DEFAULT '',
    model_id            TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','success','error','cancelled')),
    request_hash        TEXT NOT NULL DEFAULT '',
    request_payload_ref TEXT,
    response_payload_ref TEXT,
    finish_reason       TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cached_tokens       INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    error_category      TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    started_at          INTEGER,
    completed_at        INTEGER,
    trace_id            TEXT NOT NULL DEFAULT ''
);

-- ── Ops ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS payload_objects (
    payload_ref  TEXT PRIMARY KEY,
    sha256       TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_records (
    audit_id      TEXT PRIMARY KEY,
    actor_id      TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL DEFAULT '',
    target_type   TEXT NOT NULL DEFAULT '',
    target_id     TEXT NOT NULL DEFAULT '',
    changes       TEXT NOT NULL DEFAULT '{}',
    trace_id      TEXT NOT NULL DEFAULT '',
    occurred_at   TEXT NOT NULL
);

-- ── Indexes ───────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_messages_conv_seq ON messages(conversation_id, receive_sequence);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope, status, kind);
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_records(trace_id);
"""
