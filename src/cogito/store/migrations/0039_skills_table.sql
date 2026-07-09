-- 0039: skills 表 —— Skill 生命周期管理（Plan 08 Dashboard D5）。
-- 纯新增表，不影响既有数据。online_safe。

CREATE TABLE IF NOT EXISTS skills (
    skill_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','archived','deprecated')),
    version         TEXT NOT NULL DEFAULT '1.0',
    description     TEXT NOT NULL DEFAULT '',
    archived_at     TEXT,
    pinned          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK(pinned IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_skills_status ON skills(status);
