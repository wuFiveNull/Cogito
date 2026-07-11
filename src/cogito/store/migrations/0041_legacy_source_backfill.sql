-- 0041: Legacy source backfill (PLAN-13 P13-03)
-- Applied at version 41
--
-- 将 memory_items 中已有的 source_type/source_id 迁移到 memory_sources 表。
-- 幂等：基于唯一约束，重复执行不产生重复行。
--
-- - 可解析 source_id（非空、非 auto_extract）→ 建立真实来源行
-- - auto_extract / 空 → 标记 trust_label=legacy_unresolved，evidence_ref 保留原始值

INSERT OR IGNORE INTO memory_sources (
    memory_source_id, memory_id, source_type, source_id,
    source_revision, source_sequence, evidence_ref, evidence_hash,
    trust_label, extraction_id, created_at, deleted_at
)
SELECT
    mi.memory_id || '-legacy',
    mi.memory_id,
    COALESCE(NULLIF(mi.source_type, ''), 'message'),
    CASE
        WHEN mi.source_id = '' OR mi.source_id = 'auto_extract'
        THEN ''
        ELSE mi.source_id
    END,
    '',
    0,
    CASE
        WHEN mi.source_id = 'auto_extract'
        THEN 'original_source_type=' || COALESCE(mi.source_type, 'null')
        ELSE ''
    END,
    '',
    CASE
        WHEN mi.source_id = '' OR mi.source_id = 'auto_extract'
        THEN 'legacy_unresolved'
        ELSE 'medium'
    END,
    '',
    COALESCE(mi.created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    mi.deleted_at
FROM memory_items mi
WHERE mi.source_type != ''
   OR mi.source_id != '';
