-- Migration v2 → v3: StateLoadPhase control tables
--
-- Adds 3 new control tables needed by the StateLoadPhase read adapters:
--   user_profiles   — deterministic user profile data
--   user_settings   — deterministic user settings (locale, timezone, etc.)
--   session_configs — session-level configuration overrides
--
-- These tables are READ by StateLoadPhase and WRITTEN by external
-- management interfaces (user profile editor, admin panel, etc.).
-- PersistencePhase does NOT write to these tables.
--
-- Design: see state-load-phase-implementation-guide §4, §21

-- ============================================================
-- 1. User Profiles — deterministic user profile data
-- ============================================================
CREATE TABLE IF NOT EXISTS user_profiles (
    actor_id                TEXT PRIMARY KEY,

    display_name            TEXT,
    locale                  TEXT,
    timezone                TEXT,

    metadata_json           TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(metadata_json)),

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;

CREATE TRIGGER IF NOT EXISTS trg_user_profiles_touch_updated_at
AFTER UPDATE ON user_profiles
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE user_profiles
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE actor_id = NEW.actor_id;
END;

-- ============================================================
-- 2. User Settings — deterministic user settings
-- ============================================================
CREATE TABLE IF NOT EXISTS user_settings (
    actor_id                TEXT PRIMARY KEY,

    locale                  TEXT NOT NULL DEFAULT 'zh-CN',
    timezone                TEXT NOT NULL DEFAULT 'UTC',
    response_style          TEXT,
    tool_approval_mode      TEXT NOT NULL DEFAULT 'default',

    metadata_json           TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(metadata_json)),

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;

CREATE TRIGGER IF NOT EXISTS trg_user_settings_touch_updated_at
AFTER UPDATE ON user_settings
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE user_settings
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE actor_id = NEW.actor_id;
END;

-- ============================================================
-- 3. Session Configs — session-level configuration overrides
-- ============================================================
CREATE TABLE IF NOT EXISTS session_configs (
    session_id              TEXT PRIMARY KEY
                            REFERENCES sessions(session_id),

    history_limit           INTEGER NOT NULL DEFAULT 20
                            CHECK (history_limit >= 1),

    max_tool_rounds         INTEGER
                            CHECK (max_tool_rounds IS NULL
                                   OR max_tool_rounds BETWEEN 1 AND 32),

    model_profile           TEXT,

    metadata_json           TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(metadata_json)),

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;

CREATE TRIGGER IF NOT EXISTS trg_session_configs_touch_updated_at
AFTER UPDATE ON session_configs
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE session_configs
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE session_id = NEW.session_id;
END;

-- ============================================================
-- 4. Schema version
-- ============================================================
PRAGMA user_version = 3;
