-- 0019: Memory lifecycle fields — reinforcement, exposure, emotional_weight
-- Applied at version 19
--
-- 里程碑 G1: 补齐生命周期字段
--
-- retrieval_weight 不再由被动召回直接增加；
-- 由维护任务按版本化公式按版本周期重算。

ALTER TABLE memory_items ADD COLUMN reinforcement      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_items ADD COLUMN exposure_count      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_items ADD COLUMN emotional_weight    REAL    NOT NULL DEFAULT 0.5;
ALTER TABLE memory_items ADD COLUMN decay_rate         REAL    NOT NULL DEFAULT 1.0;
ALTER TABLE memory_items ADD COLUMN embedding_model     TEXT    NOT NULL DEFAULT '';
ALTER TABLE memory_items ADD COLUMN embedding_version   TEXT    NOT NULL DEFAULT '';
