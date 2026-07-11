-- 0047: proactive_cadence_state —— 自适应节拍持久化 (M2)。
-- Scheduler 持久化下一次评估触发时间，不再使用进程内 sleep loop。
-- 单例表 (id=1)；energy band 驱动间隔，misfire 使用 coalesce。
-- online_safe: 纯新增表；INSERT 初始行由 Scheduler 首次 tick 完成。

CREATE TABLE IF NOT EXISTS proactive_cadence_state (
  id           INTEGER PRIMARY KEY DEFAULT 1 CHECK(id = 1),
  last_eval_at INTEGER,                       -- epoch ms；上次实际触发时间
  next_eval_at INTEGER NOT NULL,              -- epoch ms；下次计划触发时间
  interval_s   INTEGER NOT NULL,              -- 当前计算出的评估间隔（秒）
  energy_band  TEXT NOT NULL,                 -- 决定当前间隔的能量档
  updated_at   INTEGER NOT NULL
);

-- 初始行：首次 tick 即视为到期 (next_eval_at=0)
INSERT OR IGNORE INTO proactive_cadence_state
  (id, last_eval_at, next_eval_at, interval_s, energy_band, updated_at)
VALUES (1, NULL, 0, 60, 'medium', 0);
