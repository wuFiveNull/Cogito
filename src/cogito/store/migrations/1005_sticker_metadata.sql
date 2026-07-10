-- 1005: sticker metadata on multimodal_assets (Sticker feature, image outbound).
-- Additive migration. Reuses the existing payload/sha256/pipeline — only appends
-- sticker semantics so the Agent can tag, list, and resend image assets.

ALTER TABLE multimodal_assets ADD COLUMN is_sticker INTEGER NOT NULL DEFAULT 0;
ALTER TABLE multimodal_assets ADD COLUMN sticker_name TEXT NOT NULL DEFAULT '';
ALTER TABLE multimodal_assets ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE multimodal_assets ADD COLUMN usage_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_multimodal_assets_sticker
    ON multimodal_assets(is_sticker, created_at DESC) WHERE is_sticker = 1;
