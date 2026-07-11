-- 0046: proactive_decisions_v2 energy 活动快照 + 模型版本字段。
-- M1: energy 接入真实用户活动，Decision 保存当时 activity 快照。
-- 注意：config_version_id 已在 0036 中新增，本迁移不再重复。
-- online_safe: 仅 ADD COLUMN（DEFAULT 值），不影响已有数据。

ALTER TABLE proactive_decisions_v2
    ADD COLUMN last_user_at INTEGER;            -- epoch ms；决定时真实用户活动快照（NULL=从未活动）

ALTER TABLE proactive_decisions_v2
    ADD COLUMN energy_model_version TEXT NOT NULL DEFAULT 'v1';  -- 能量模型版本
