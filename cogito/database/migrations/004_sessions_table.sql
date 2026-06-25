-- v4: 为 sessions 表增加 title 列（供 Web Channel 显示会话标题）
--
-- sessions 表已在 v2 迁移中创建，包含 session_id, user_id, created_at 等字段。
-- v4 增加 title 列用于 Web 界面展示会话标题。

-- SQLite 不支持 ALTER TABLE ADD COLUMN IF NOT EXISTS，
-- 用 PRAGMA table_info 先检查列是否存在。
-- 此处的执行由 migrate_v4 函数包装，异常被静默捕获。

ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT '';
