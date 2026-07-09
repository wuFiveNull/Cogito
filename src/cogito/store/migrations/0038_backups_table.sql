-- 0030: backups 表 —— 备份记录持久化（Plan 08 Dashboard D6 / D7）。
-- 纯新增表，不影响既有数据。online_safe。

CREATE TABLE IF NOT EXISTS backups (
    backup_id       TEXT PRIMARY KEY,
    path            TEXT NOT NULL,
    size_mb         REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','completed','failed','verified')),
    verified        INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL DEFAULT 'full'
                    CHECK(kind IN ('full','incremental','config')),
    CHECK(verified IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_backups_created
    ON backups(created_at DESC);
