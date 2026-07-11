-- 0052: memory_items 擦除 tombstone 元数据 (PLAN-16 M3 MEM-05)
-- 为显式 erase-memory 补充最小 tombstone 字段：擦除原因 + Receipt 引用。
-- online_safe: 仅 ADD COLUMN（DEFAULT NULL），不影响已有数据。
--
-- 擦除语义：value/subject/predicate 清空、status='expired'、deleted_at 置位，
-- 同时写入 erasure_reason + receipt_id，保留 memory_id / principal_id / scope /
-- 时间戳等最小 tombstone 供对账审计。

ALTER TABLE memory_items
    ADD COLUMN erasure_reason TEXT DEFAULT NULL;

ALTER TABLE memory_items
    ADD COLUMN receipt_id TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_receipt
    ON memory_items(receipt_id) WHERE receipt_id IS NOT NULL;
