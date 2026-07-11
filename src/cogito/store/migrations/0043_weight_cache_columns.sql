-- 0043: Memory weight cache columns (PLAN-13 P13-05)
-- Applied at version 43
--
-- 新增 retrieval_weight 缓存写回所需的元数据列：
-- - last_weight_update: 上次权重计算时间
-- - algorithm_version: 计算权重的算法/策略版本
--
-- 这两个列是 retrieval_weight 缓存的版本标记，唯一事实是 memory_signals 表。

ALTER TABLE memory_items ADD COLUMN last_weight_update     TEXT;
ALTER TABLE memory_items ADD COLUMN algorithm_version       TEXT NOT NULL DEFAULT '1';
