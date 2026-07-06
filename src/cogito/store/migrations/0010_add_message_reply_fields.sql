-- 0010: Add reply_route and capability_snapshot JSON columns to messages
-- Applied at version 10
--
-- Plan 01 / 2.4: 入站时保存不可变 JSON 快照，Delivery 从输入 Message 复制 Reply Route。
-- Agent Loop 不读取和修改 Reply Route。

-- Add reply_route_json for storing the ReplyRoute snapshot from the incoming envelope
ALTER TABLE messages ADD COLUMN reply_route_json TEXT NOT NULL DEFAULT '{}';

-- Add capability_snapshot_json for storing the capability snapshot from the incoming envelope
ALTER TABLE messages ADD COLUMN capability_snapshot_json TEXT NOT NULL DEFAULT '{}';
