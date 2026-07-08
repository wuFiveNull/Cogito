-- 会话软删除：添加 deleted_at 列
-- 已有会话默认 deleted_at = NULL（未删除）
ALTER TABLE sessions ADD COLUMN deleted_at TEXT;
