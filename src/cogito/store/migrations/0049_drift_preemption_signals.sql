-- 0049: drift_preemption_signals —— Drift 抢占信号 (M5)。
-- 新 Turn 入站后置位；Drift 单步前检查并消费清除。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS drift_preemption_signals (
  principal_id      TEXT PRIMARY KEY,
  preempt_requested  INTEGER NOT NULL DEFAULT 0,
  requested_at      INTEGER NOT NULL DEFAULT 0,
  reason            TEXT
);
