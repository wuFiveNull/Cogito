-- 0031: capabilities 表 —— Capability Registry 运行期快照持久化（Plan 03 M1）。
-- 启动阶段注册结果落盘，运行时按 Principal/mode/Policy 过滤。
-- online_safe: 纯新增表。

CREATE TABLE IF NOT EXISTS capabilities (
    capability_id       TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    version             TEXT NOT NULL,
    owner               TEXT,
    provider            TEXT,
    plugin_id           TEXT,
    toolsets            TEXT NOT NULL DEFAULT '[]',
    supported_modes     TEXT NOT NULL DEFAULT '[]',
    input_schema        TEXT,
    output_schema       TEXT,
    permissions         TEXT NOT NULL DEFAULT '[]',
    risk_level          TEXT NOT NULL DEFAULT 'low',
    side_effect_class   TEXT NOT NULL DEFAULT 'none',
    resource_requirements TEXT NOT NULL DEFAULT '{}',
    health              TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(health IN ('unknown','healthy','degraded','unavailable')),
    disabled            INTEGER NOT NULL DEFAULT 0,
    deprecated          INTEGER NOT NULL DEFAULT 0,
    discovered_at       INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capabilities_health
    ON capabilities(health) WHERE health != 'healthy';

CREATE INDEX IF NOT EXISTS idx_capabilities_plugin
    ON capabilities(plugin_id) WHERE plugin_id IS NOT NULL;
