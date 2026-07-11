-- 0051: proactive_candidates.origin —— 区分 Candidate 来源 (R9 M6)。
-- origin = 'connector' | 'feedback' | 'drift' | 'manual' 等；NULL 表示遗留。
-- Drift 结果投影为 Candidate 时必须标记 origin='drift'，以便追溯。
-- online_safe: 仅 ADD COLUMN（DEFAULT NULL），不影响已有数据。

ALTER TABLE proactive_candidates
    ADD COLUMN origin TEXT DEFAULT NULL
        CHECK (origin IS NULL OR origin IN (
            'connector', 'feedback', 'drift', 'manual', 'alert_fastpath'));

CREATE INDEX IF NOT EXISTS idx_candidates_origin
    ON proactive_candidates(principal_id, origin);
