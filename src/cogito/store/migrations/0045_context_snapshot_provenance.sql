-- 0045: persist PLAN-13 per-source budget and item provenance.
ALTER TABLE context_snapshots ADD COLUMN per_source_tokens_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE context_snapshots ADD COLUMN exclusion_stats_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE context_snapshot_items ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}';
