-- 0060: drift_runs active partial unique index —— 同时只允许 1 个 active Drift / 主 (PLAN-17 R6 DR-P1-02)。
-- status IN ('admitted','running','waiting','paused') 的行按 principal_id 唯一。
-- online_safe: 纯新增部分索引; 旧数据无影响。

CREATE UNIQUE INDEX IF NOT EXISTS uq_drift_one_active_per_principal
  ON drift_runs(principal_id)
  WHERE status IN ('admitted','running','waiting','paused');
