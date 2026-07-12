-- 0057: drift_rums 选择追溯 —— selection_trace_json + selector_version (PLAN-17 R1 P0-01)。
-- Scheduler 在 admission 时调用 resolve_catalog + select_skill，把真实选择的
-- skill_name/score 持久化到 drift_runs，替代 "(selected-at-run)" 占位符。
-- online_safe: 仅 ADD COLUMN（DEFAULT NULL），不影响已有数据。

ALTER TABLE drift_runs
    ADD COLUMN selection_trace_json TEXT;  -- {"weights_version":"1","scores":{name:score}}

ALTER TABLE drift_runs
    ADD COLUMN selector_version TEXT;       -- 评分权重版本（便于跨版本比较）
