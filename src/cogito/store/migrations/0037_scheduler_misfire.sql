-- 0037: schedules 表增加 last_fired_at / normalized_interval_s / dst_policy 列（Plan 04 M2）。
-- 支持 misfire 检测、DST 确定策略、间隔估算。
-- online_safe: 仅新增可空列。

ALTER TABLE schedules ADD COLUMN last_fired_at INTEGER DEFAULT NULL;
ALTER TABLE schedules ADD COLUMN normalized_interval_s INTEGER DEFAULT NULL;
ALTER TABLE schedules ADD COLUMN dst_policy TEXT NOT NULL DEFAULT 'post';

CREATE INDEX IF NOT EXISTS idx_schedules_last_fired
    ON schedules(last_fired_at) WHERE last_fired_at IS NOT NULL;
