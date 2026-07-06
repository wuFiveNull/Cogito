-- 0011: Add endpoint_ref and conversation_endpoint_ref for ref-based identity lookup
-- Applied at version 11
--
-- Plan 01 / 2.2: 优先使用 sender_endpoint_ref / conversation_endpoint_ref 查找身份，
-- Ref 为空时退回现有 platform ID 查找。

ALTER TABLE endpoints ADD COLUMN endpoint_ref TEXT NOT NULL DEFAULT '';

ALTER TABLE conversations ADD COLUMN conversation_endpoint_ref TEXT NOT NULL DEFAULT '';
