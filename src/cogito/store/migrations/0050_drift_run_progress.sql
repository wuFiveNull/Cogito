-- 0050: drift_runs 进度列 —— budget 累计 + steps 计数 (R7 M5 执行接线)。
-- online_safe: 仅 ADD COLUMN（DEFAULT 值），不影响已有数据。

ALTER TABLE drift_runs
    ADD COLUMN budget_used_json TEXT NOT NULL DEFAULT '{}';  -- {"tool_calls":N,"model_calls":N}

ALTER TABLE drift_runs
    ADD COLUMN steps_taken INTEGER NOT NULL DEFAULT 0;
