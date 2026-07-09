-- 0032: context_snapshots + context_snapshot_items —— Context Snapshot 持久化（Plan 02 M5）。
-- 记录每个 Attempt 构建的上下文快照，含条目来源/分数/Token/检索路径。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS context_snapshots (
    snapshot_id             TEXT PRIMARY KEY,
    session_id              TEXT NOT NULL,
    attempt_id              TEXT,
    attempt_type            TEXT NOT NULL DEFAULT 'run'
                            CHECK(attempt_type IN ('run','task')),
    parent_snapshot_id      TEXT,
    message_upper_bound     INTEGER,
    query_plan_version      TEXT,
    selection_policy_version TEXT,
    token_budget            INTEGER NOT NULL,
    tokens_used             INTEGER NOT NULL DEFAULT 0,
    excluded_summary        INTEGER NOT NULL DEFAULT 0,
    created_at              INTEGER NOT NULL,
    schema_version          TEXT NOT NULL DEFAULT '1'
);

CREATE TABLE IF NOT EXISTS context_snapshot_items (
    snapshot_id         TEXT NOT NULL,
    item_index          INTEGER NOT NULL,
    source              TEXT NOT NULL,
    score               REAL,
    tokens              INTEGER,
    trust_label         TEXT,
    retrieval_path      TEXT,
    content_ref         TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, item_index),
    FOREIGN KEY (snapshot_id) REFERENCES context_snapshots(snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_session
    ON context_snapshots(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_attempt
    ON context_snapshots(attempt_type, attempt_id);
