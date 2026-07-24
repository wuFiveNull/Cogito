-- Contract phase: Event log is the sole Delivery fact source.
-- Run only in maintenance mode after a verified backup.
DROP TABLE IF EXISTS delivery_receipts;
DROP TABLE IF EXISTS delivery_attempts;
DROP TABLE IF EXISTS deliveries;
