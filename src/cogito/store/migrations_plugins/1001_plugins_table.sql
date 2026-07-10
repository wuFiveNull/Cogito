-- 0001: plugins 表 —— Plugin Runtime 状态持久化（PLAN-10 M5）。
-- 纯新增表，不影响既有数据。online_safe。rollback: DROP TABLE plugins。

CREATE TABLE IF NOT EXISTS plugins (
    plugin_id       TEXT PRIMARY KEY,
    version         TEXT NOT NULL DEFAULT '1.0',
    api_version     TEXT NOT NULL DEFAULT '1',
    status          TEXT NOT NULL DEFAULT 'discovered'
                    CHECK(status IN (
                        'discovered','validated','installed','configured',
                        'enabled','running','degraded','disabled','stopped'
                    )),
    source          TEXT NOT NULL DEFAULT 'builtin'
                    CHECK(source IN ('builtin','user','project','pip')),
    source_path     TEXT NOT NULL DEFAULT '',
    entry_point     TEXT NOT NULL DEFAULT '',
    permissions     TEXT NOT NULL DEFAULT '[]',
    install_hash    TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    fail_count      INTEGER NOT NULL DEFAULT 0,
    last_fail_at    TEXT,
    started_at      TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plugins_status ON plugins(status, source);
