-- 0030: side_effect_receipts 表 —— Tool 副作用 Receipt 持久化（Plan 03 M3）。
-- 记录每个 Tool 执行的外部操作 ID、请求哈希、状态与对账结果。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS side_effect_receipts (
    receipt_id          TEXT PRIMARY KEY,
    capability_id       TEXT NOT NULL,
    operation_id        TEXT,
    request_hash        TEXT NOT NULL,
    side_effect_class   TEXT NOT NULL
                        CHECK(side_effect_class IN ('none','idempotent','reconcilable','non_retriable')),
    status              TEXT NOT NULL
                        CHECK(status IN ('pending','success','failed','unknown')),
    reconcile_status    TEXT NOT NULL DEFAULT 'not_needed'
                        CHECK(reconcile_status IN ('not_needed','pending','reconciled','dead_letter')),
    raw_ref             TEXT,
    summary             TEXT,
    attempt_id          TEXT NOT NULL,
    attempt_type        TEXT NOT NULL DEFAULT 'run'
                        CHECK(attempt_type IN ('run','task')),
    created_at          INTEGER NOT NULL,
    resolved_at         INTEGER,
    audit_id            TEXT
);

CREATE INDEX IF NOT EXISTS idx_receipts_attempt
    ON side_effect_receipts(attempt_type, attempt_id);

CREATE INDEX IF NOT EXISTS idx_receipts_reconcile
    ON side_effect_receipts(reconcile_status)
    WHERE reconcile_status IN ('pending','dead_letter');

CREATE INDEX IF NOT EXISTS idx_receipts_capability
    ON side_effect_receipts(capability_id, created_at DESC);
