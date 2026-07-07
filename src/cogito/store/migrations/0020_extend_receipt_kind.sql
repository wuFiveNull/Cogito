-- 0020: Extend delivery_receipts CHECK constraint to include 'temporary' and 'permanent'
-- QQ-ONEBOT-E2E-01 / PR 4: DeliveryWorker 需要区分 temporary/permanent 结果
--
-- SQLite 不支持 ALTER TABLE 修改 CHECK 约束，需要：
-- 1. 重命名旧表
-- 2. 创建新表（扩展 CHECK）
-- 3. 迁移数据
-- 4. 删除旧表

PRAGMA foreign_keys=OFF;

ALTER TABLE delivery_receipts RENAME TO delivery_receipts_v0008;

CREATE TABLE delivery_receipts (
    receipt_id          TEXT PRIMARY KEY,
    delivery_id         TEXT NOT NULL REFERENCES deliveries(delivery_id),
    delivery_attempt_id TEXT NOT NULL DEFAULT '',
    operation_seq       INTEGER NOT NULL DEFAULT 1,
    request_hash        TEXT NOT NULL DEFAULT '',
    receipt_kind        TEXT NOT NULL DEFAULT 'uncertain'
                        CHECK(receipt_kind IN ('confirmed', 'uncertain', 'reconciled', 'temporary', 'permanent')),
    platform_message_id TEXT,
    safe_result         TEXT,
    observed_at         INTEGER NOT NULL,
    lease_version       INTEGER NOT NULL DEFAULT 1,
    UNIQUE(delivery_id, delivery_attempt_id, operation_seq)
);

INSERT INTO delivery_receipts
    (receipt_id, delivery_id, delivery_attempt_id, operation_seq, request_hash,
     receipt_kind, platform_message_id, safe_result, observed_at, lease_version)
SELECT receipt_id, delivery_id, delivery_attempt_id, operation_seq, request_hash,
       receipt_kind, platform_message_id, safe_result, observed_at, lease_version
FROM delivery_receipts_v0008;

DROP TABLE delivery_receipts_v0008;

CREATE INDEX IF NOT EXISTS idx_receipts_delivery ON delivery_receipts(delivery_id);
CREATE INDEX IF NOT EXISTS idx_receipts_kind ON delivery_receipts(receipt_kind);

PRAGMA foreign_keys=ON;
