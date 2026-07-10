-- 1002: durable Gateway operation idempotency receipts (PLAN-11 M2).
-- Gateway operational state is separate from the Core Delivery aggregate.

CREATE TABLE IF NOT EXISTS gateway_operation_receipts (
    operation_key       TEXT PRIMARY KEY,
    action              TEXT NOT NULL,
    response_json       TEXT NOT NULL,
    created_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gateway_operation_action_time
    ON gateway_operation_receipts(action, created_at DESC);
