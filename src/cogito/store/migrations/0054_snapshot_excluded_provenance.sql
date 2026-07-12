-- 0054: context_snapshots.excluded_json —— 被排除候选完整 provenance（PLAN-16 M6 #14）。
-- excluded candidate 列表（含 score 分项与排除原因），供对账与可解释性。
-- online_safe: ADD COLUMN DEFAULT NULL，不影响已有数据。

ALTER TABLE context_snapshots
    ADD COLUMN excluded_json TEXT DEFAULT NULL;
