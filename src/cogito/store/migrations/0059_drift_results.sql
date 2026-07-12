-- 0059: drift_results —— Drift 完成结果持久化 + Candidate 投影 (PLAN-17 R5 P0-06)。
-- Skill 执行完毕后 Drift Handler 在同一事务写 DriftResult + Outbox DriftResultCommitted；
-- Consumer 校验 completed/principal/can_emit_candidate/allow_candidate_emission 后
-- 调 DriftProjectionService 写 ProactiveCandidate(origin=drift)。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS drift_results (
  drift_result_id      TEXT PRIMARY KEY,
  drift_run_id         TEXT NOT NULL REFERENCES drift_runs(drift_run_id),
  task_attempt_id      TEXT NOT NULL,
  result_kind          TEXT NOT NULL,          -- 'internal_only' | 'candidate_emission' | 'skipped_no_value'
  result_ref           TEXT NOT NULL,          -- drift-check:<run>:<step> 引用
  summary              TEXT,
  items_json           TEXT NOT NULL DEFAULT '[]',  -- 内部项 JSON
  candidate_draft_json TEXT,                   -- 候选草稿（仅当 can_emit_candidate 等条件满足时填入）
  candidate_id         TEXT,                   -- 投影成功后回写
  emitted              INTEGER NOT NULL DEFAULT 0, -- 0 未投影 / 1 已投影
  created_at           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_results_run
  ON drift_results(drift_run_id, created_at DESC);
