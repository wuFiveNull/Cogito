-- 0058: task_checkpoints —— Drift 检查点版本化历史 (PLAN-17 R3 P0-03/04)。
-- Drift Checkpoint V1（schema_version=1）写入此表；同时 tasks /
-- task_attempts / drift_skill_state 上 inline 存最新一份 JSON 供快速读取。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS task_checkpoints (
  checkpoint_id           TEXT PRIMARY KEY,
  task_id                 TEXT NOT NULL REFERENCES tasks(task_id),
  task_attempt_id         TEXT NOT NULL,
  drift_run_id            TEXT,
  checkpoint_type         TEXT NOT NULL,           -- 'drift-step' / 'drift-pause' / 'drift-finish'
  schema_version          INTEGER NOT NULL,
  payload_ref             TEXT NOT NULL,           -- drift-check:<run>:<step> 风格引用
  payload_json            TEXT NOT NULL,           -- 内联 JSON 主体（小 payload）
  payload_hash            TEXT NOT NULL,           -- sha256(payload_json) 校验
  config_version_id       TEXT,
  capability_snapshot_version TEXT,
  created_at              INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_checkpoints_latest
  ON task_checkpoints(task_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_task_checkpoints_run
  ON task_checkpoints(drift_run_id, created_at DESC);
