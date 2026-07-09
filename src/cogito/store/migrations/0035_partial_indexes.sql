-- 0035: 部分索引补齐（Plan 06 M5）。
-- 为高频查询路径添加部分索引，减少全表扫描。
-- online_safe: 仅新增索引，不影响数据。

-- 活跃 Session 中的运行/等待 Turn
CREATE INDEX IF NOT EXISTS idx_turns_active_session
    ON turns(session_id, created_at)
    WHERE status IN ('waiting_user','waiting_external','running');

-- 待执行 Task（Worker 领取队列）
CREATE INDEX IF NOT EXISTS idx_tasks_queued
    ON tasks(status, priority, scheduled_at)
    WHERE status = 'queued';

-- 待发布 Outbox Event
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_events(status, aggregate_version)
    WHERE status = 'pending';

-- 在途 Delivery（含 unknown 待对账）
CREATE INDEX IF NOT EXISTS idx_deliveries_inflight
    ON deliveries(status, created_at)
    WHERE status IN ('sending','unknown','streaming','finalizing');

-- 待响应 Approval
CREATE INDEX IF NOT EXISTS idx_approvals_pending
    ON approvals(status, expires_at)
    WHERE status = 'pending';

-- 活跃 Connector Schedule
CREATE INDEX IF NOT EXISTS idx_schedules_due
    ON schedules(enabled, next_fire_at)
    WHERE enabled = 1;
