-- 0008: Add delivery_receipts table for durable Receipt tracking
-- Applied at version 8
--
-- ACCESS-DELIVERY / 4.4 Delivery Attempt
-- 每次投递尝试保存请求 Hash、平台结果和错误信息。
--
-- DATABASE-SCHEMA / 2. 表分组
-- Delivery 聚合包含 delivery_receipts。
--
-- Receipt 作用：
-- - confirmed: 外部发送明确成功，本地事务成功提交
-- - uncertain: 外部已发送但本地事务无法确认（Lease 过期、版本变化、崩溃）
-- - reconciled: 人工/自动对账后确认平台结果

PRAGMA foreign_keys=OFF;

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

CREATE INDEX IF NOT EXISTS idx_receipts_delivery ON delivery_receipts(delivery_id);
CREATE INDEX IF NOT EXISTS idx_receipts_kind ON delivery_receipts(receipt_kind);

PRAGMA foreign_keys=ON;
