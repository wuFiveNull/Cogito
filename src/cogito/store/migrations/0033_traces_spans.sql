-- 0033: traces + spans —— 分布式追踪持久化（Plan 07 可观测性）。
-- 每个 Turn/Attempt/Tool/Model/Delivery 操作记录为 span，归属到 trace。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS traces (
    trace_id            TEXT PRIMARY KEY,
    actor               TEXT,
    origin              TEXT,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    status              TEXT NOT NULL
                        CHECK(status IN ('running','ok','error','cancelled'))
);

CREATE TABLE IF NOT EXISTS spans (
    span_id             TEXT PRIMARY KEY,
    trace_id            TEXT NOT NULL,
    parent_span_id      TEXT,
    name                TEXT NOT NULL,
    kind                TEXT NOT NULL
                        CHECK(kind IN ('turn','attempt','tool','model','delivery','retrieval','sandbox','custom')),
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    status              TEXT NOT NULL
                        CHECK(status IN ('running','ok','error','cancelled')),
    attributes          TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
);

CREATE INDEX IF NOT EXISTS idx_spans_trace
    ON spans(trace_id, started_at);

CREATE INDEX IF NOT EXISTS idx_spans_parent
    ON spans(parent_span_id) WHERE parent_span_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_traces_status
    ON traces(status, started_at) WHERE status = 'running';
