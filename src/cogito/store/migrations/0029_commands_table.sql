-- 0029: commands 表 —— Command Envelope 审计持久化（Plan 04 M4 / Plan 05 M4）。
-- 所有写操作通过 Command API 落盘，支持幂等键去重与状态追溯。
-- online_safe: 纯新增表，不影响既有数据。

CREATE TABLE IF NOT EXISTS commands (
    command_id          TEXT PRIMARY KEY,
    actor               TEXT NOT NULL,
    command_type        TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    target_type         TEXT,
    target_id           TEXT,
    expected_version    INTEGER,
    payload             TEXT,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','consumed','rejected','expired','idempotency_conflict')),
    result_summary      TEXT,
    error_code          TEXT,
    created_at          INTEGER NOT NULL,
    expires_at          INTEGER,
    consumed_at         INTEGER,
    origin              TEXT,
    trace_id            TEXT
);

-- 幂等键唯一约束：同一 actor + 类型 + 幂等键 只允许一条
CREATE UNIQUE INDEX IF NOT EXISTS idx_commands_idempotency
    ON commands(actor, command_type, idempotency_key);

-- 待处理命令查询
CREATE INDEX IF NOT EXISTS idx_commands_status
    ON commands(status, created_at);
