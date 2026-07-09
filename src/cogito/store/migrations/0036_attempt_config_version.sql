-- 0036: run_attempts / task_attempts 增加 config_version_id 外键（Plan 06 M2）。
-- 让每次执行都能追溯创建时使用的 config hash。
-- online_safe: 仅新增可空列。

ALTER TABLE run_attempts ADD COLUMN config_version_id TEXT
    REFERENCES config_versions(version_id);

ALTER TABLE task_attempts ADD COLUMN config_version_id TEXT
    REFERENCES config_versions(version_id);

-- proactive_decisions_v02 是实际使用的决策表（0026 迁移创建）
ALTER TABLE proactive_decisions_v2 ADD COLUMN config_version_id TEXT
    REFERENCES config_versions(version_id);
