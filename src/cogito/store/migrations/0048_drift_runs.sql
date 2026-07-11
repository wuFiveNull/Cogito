-- 0048: drift_runs + drift_skill_state —— Drift MVP 持久化 (M3)。
-- Drift 复用 tasks/task_attempts 作为生命周期权威；drift_runs.status 是查询
-- 投影，必须由同一事务或 Event Consumer 更新。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS drift_runs (
  drift_run_id          TEXT PRIMARY KEY,
  task_id               TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
  principal_id          TEXT NOT NULL DEFAULT 'owner',
  skill_name            TEXT NOT NULL,
  skill_version         TEXT NOT NULL,
  status                TEXT NOT NULL
                          CHECK(status IN (
                            'admitted','running','waiting','paused',
                            'completed','failed','needs_review')),
  admission_snapshot_json TEXT NOT NULL,
  finish_summary        TEXT,
  result_ref            TEXT,
  candidate_id          TEXT,
  preemption_reason     TEXT,
  started_at            INTEGER,
  finished_at           INTEGER,
  created_at            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_runs_principal_status
  ON drift_runs(principal_id, status);
CREATE INDEX IF NOT EXISTS idx_drift_runs_task
  ON drift_runs(task_id);

CREATE TABLE IF NOT EXISTS drift_skill_state (
  principal_id    TEXT NOT NULL,
  skill_name      TEXT NOT NULL,
  skill_version   TEXT NOT NULL,
  last_status     TEXT,
  last_run_at     INTEGER,
  run_count       INTEGER NOT NULL DEFAULT 0,
  checkpoint_ref  TEXT,
  cursor_json     TEXT,
  updated_at      INTEGER NOT NULL,
  PRIMARY KEY (principal_id, skill_name)
);
