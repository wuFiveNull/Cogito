-- 0023: 流式 Delivery 关联 Turn（崩溃恢复用，Plan 05 M5）
-- 流式 Delivery 在 AgentRunner.run_once 内由 Turn 的 RunAttempt lease 拥有。
-- 进程崩溃后，该 Delivery 会永久卡在 status='streaming'（平台已创建占位气泡），
-- 而其 Turn/RunAttempt 由 recover_stale_turns 标记为 abandoned / queued。
-- 新增 turn_id 列使恢复扫描能直接定位"孤儿"流式 Delivery 并撤回。
-- 所有新列均有默认值，迁移幂等。

ALTER TABLE deliveries ADD COLUMN turn_id TEXT;

CREATE INDEX IF NOT EXISTS idx_deliveries_turn_id ON deliveries(turn_id);
