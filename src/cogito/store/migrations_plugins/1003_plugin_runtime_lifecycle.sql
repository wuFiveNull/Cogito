-- 1003: Plugin Runtime lifecycle snapshots and audit (PLAN-11 M3).

ALTER TABLE plugins ADD COLUMN isolation TEXT NOT NULL DEFAULT 'subprocess';
ALTER TABLE plugins ADD COLUMN trusted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plugins ADD COLUMN updated_at TEXT;

CREATE TABLE IF NOT EXISTS plugin_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    plugin_id       TEXT NOT NULL,
    manifest_json   TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plugin_snapshots_plugin_time
    ON plugin_snapshots(plugin_id, created_at DESC);

CREATE TABLE IF NOT EXISTS plugin_runtime_audit (
    audit_id        TEXT PRIMARY KEY,
    plugin_id       TEXT NOT NULL,
    action          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    safe_detail     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plugin_runtime_audit_plugin_time
    ON plugin_runtime_audit(plugin_id, created_at DESC);
