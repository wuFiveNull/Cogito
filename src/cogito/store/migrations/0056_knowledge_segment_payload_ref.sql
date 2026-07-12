-- 0056: knowledge_segments.payload_ref —— 大正文内容寻址 (PLAN-16 M4 完整 payload 边界)
-- 当段落正文超过阈值，正文写入正文写入 PayloadStore，段落仅保存 payload_ref + 空 inline。
-- online_safe: ADD COLUMN DEFAULT NULL，不影响已有数据。

ALTER TABLE knowledge_segments
    ADD COLUMN payload_ref TEXT DEFAULT NULL;

-- payload_ref 引用完整性：当资源删除时级联清除（由应用层保证，此处加索引便于查询）
CREATE INDEX IF NOT EXISTS idx_ks_payload_ref
    ON knowledge_segments(payload_ref) WHERE payload_ref IS NOT NULL;
