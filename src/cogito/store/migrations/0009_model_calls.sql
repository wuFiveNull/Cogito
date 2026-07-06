-- 0009: Add model_calls table for call observability
-- Applied at version 9
--
-- MODEL-ADAPTER: 模型调用可审计但不泄漏 Prompt、原始错误和 Secret。
-- 每 Provider 调用有独立记录，重试共享 correlation。
-- Prompt 和原始响应只保存受限 Payload 引用。

PRAGMA foreign_keys=OFF;

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

CREATE INDEX IF NOT EXISTS idx_model_calls_attempt ON model_calls(attempt_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_trace ON model_calls(trace_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_started ON model_calls(started_at);

PRAGMA foreign_keys=ON;
