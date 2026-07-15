-- Align Receipt states with TOOL-SANDBOX / 9 reconciliation outcomes.

ALTER TABLE side_effect_receipts RENAME TO side_effect_receipts_legacy_0067;

CREATE TABLE side_effect_receipts (
    receipt_id          TEXT PRIMARY KEY,
    capability_id       TEXT NOT NULL,
    operation_id        TEXT,
    request_hash        TEXT NOT NULL,
    side_effect_class   TEXT NOT NULL
                        CHECK(side_effect_class IN ('none','idempotent','reconcilable','non_retriable')),
    status              TEXT NOT NULL
                        CHECK(status IN ('pending','succeeded','failed','unknown')),
    reconcile_status    TEXT NOT NULL DEFAULT 'not_needed'
                        CHECK(reconcile_status IN (
                            'not_needed','pending','reconciled','not_executed',
                            'manual_required','dead_letter'
                        )),
    raw_ref             TEXT,
    summary             TEXT,
    attempt_id          TEXT NOT NULL,
    attempt_type        TEXT NOT NULL DEFAULT 'run'
                        CHECK(attempt_type IN ('run','task')),
    created_at          INTEGER NOT NULL,
    resolved_at         INTEGER,
    audit_id            TEXT
);

INSERT INTO side_effect_receipts (
    receipt_id,capability_id,operation_id,request_hash,side_effect_class,status,
    reconcile_status,raw_ref,summary,attempt_id,attempt_type,created_at,resolved_at,audit_id
)
SELECT
    receipt_id,capability_id,operation_id,request_hash,side_effect_class,
    CASE status WHEN 'success' THEN 'succeeded' ELSE status END,
    reconcile_status,raw_ref,summary,attempt_id,attempt_type,created_at,resolved_at,audit_id
FROM side_effect_receipts_legacy_0067;

DROP TABLE side_effect_receipts_legacy_0067;

CREATE INDEX idx_receipts_attempt
    ON side_effect_receipts(attempt_type, attempt_id);
CREATE INDEX idx_receipts_reconcile
    ON side_effect_receipts(reconcile_status)
    WHERE reconcile_status IN ('pending','dead_letter','manual_required');
CREATE INDEX idx_receipts_capability
    ON side_effect_receipts(capability_id, created_at DESC);
