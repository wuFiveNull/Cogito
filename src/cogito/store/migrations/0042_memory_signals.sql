-- 0042: memory_signals 追加式事件表 (PLAN-13 P13-04)
-- Applied at version 42
--
-- 强化/展示/反馈使用幂等追加事件表。
-- memory_items.reinforcement 为聚合投影（非唯一事实）。
--
-- signal_type:
--   exposed          进入 Context 或工具结果
--   referenced       模型引用但用户未确认
--   user_affirmed    用户明确确认有用
--   task_succeeded   成功 Task 有可验证依赖
--   user_corrected   用户纠正并确认新事实
--   negative_feedback 用户负面反馈

CREATE TABLE IF NOT EXISTS memory_signals (
    signal_id          TEXT PRIMARY KEY,
    memory_id          TEXT NOT NULL REFERENCES memory_items(memory_id),
    signal_type        TEXT NOT NULL
        CHECK(signal_type IN (
            'exposed','referenced','user_affirmed',
            'task_succeeded','user_corrected','negative_feedback'
        )),
    signal_value       INTEGER NOT NULL DEFAULT 0,
    actor_principal_id TEXT NOT NULL DEFAULT '',
    turn_id            TEXT NOT NULL DEFAULT '',
    task_id            TEXT NOT NULL DEFAULT '',
    idempotency_key    TEXT NOT NULL DEFAULT '',
    algorithm_version  TEXT NOT NULL DEFAULT '',
    occurred_at        TEXT NOT NULL,
    metadata_json      TEXT NOT NULL DEFAULT '{}'
);

-- 幂等键唯一约束（仅对非空 idempotency_key 生效，允许多次无键写入）
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_idemp
    ON memory_signals(idempotency_key) WHERE idempotency_key != '';

CREATE INDEX IF NOT EXISTS idx_signal_memory
    ON memory_signals(memory_id, signal_type);

CREATE INDEX IF NOT EXISTS idx_signal_occurred
    ON memory_signals(occurred_at);
