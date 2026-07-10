"""Tests for strict configuration model."""

import os
import tempfile

import pytest

from cogito.config import Config, ConfigError, _mask_sensitive


class TestDefaultConfig:
    def test_default_loads_without_file(self):
        """Loading without a config file returns full defaults."""
        c = Config.load("/nonexistent/path/config.toml")
        assert c.workspace_path == ".workspace"
        assert c.storage.db_path == "data/cogito.db"
        assert c.storage.enable_wal is True
        assert c.storage.busy_timeout == 5000
        assert c.storage.payload_dir == "data/payload"
        # Resolved paths
        assert "cogito.db" in c.resolve_db_path()
        assert "payload" in c.resolve_payload_dir()
        assert "logs" in c.resolve_log_dir()

    def test_default_config_repr(self):
        c = Config()
        r = repr(c)
        assert "Config(" in r
        assert "StorageConfig(" in r
        assert "RuntimeConfig(" in r
        assert "InteractionConfig(" in r
        assert "bind_host" in r

    def test_path_resolution(self):
        c = Config()
        # Use os.sep for cross-platform path comparison
        assert c.resolve_db_path() == os.path.join(".workspace", "data", "cogito.db")
        assert c.resolve_payload_dir() == os.path.join(".workspace", "data", "payload")
        assert c.resolve_log_dir() == os.path.join(".workspace", "logs")


class TestConfigValidation:
    def test_unknown_section_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[unknown_section]\nfoo = 1\n')
            tmp = f.name
        try:
            with pytest.raises(ConfigError, match=r"unknown sections/keys: unknown_section"):
                Config.load(tmp)
        finally:
            os.unlink(tmp)

    def test_unknown_field_in_storage_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[storage]\nunknown_field = 1\n')
            tmp = f.name
        try:
            with pytest.raises(ConfigError, match=r"unknown fields: unknown_field"):
                Config.load(tmp)
        finally:
            os.unlink(tmp)

    def test_unknown_field_in_model_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[model]\nprovider = "openai_compat"\nenable_thinking = true\n')
            tmp = f.name
        try:
            with pytest.raises(
                ConfigError,
                match=r"unknown fields: enable_thinking",
            ):
                Config.load(tmp)
        finally:
            os.unlink(tmp)

    def test_known_fields_ok(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[storage]\ndb_path = "custom.db"\nenable_wal = false\n')
            tmp = f.name
        try:
            c = Config.load(tmp)
            assert c.storage.db_path == "custom.db"
            assert c.storage.enable_wal is False
        finally:
            os.unlink(tmp)


class TestSensitiveFieldMasking:
    def test_mask_api_key(self):
        assert _mask_sensitive("api_key", "sk-abc123def") == "sk-a****"
        assert _mask_sensitive("api_key", "ab") == "****"
        assert _mask_sensitive("API_KEY", "secret-value") == "secr****"

    def test_mask_token(self):
        assert _mask_sensitive("token", "my-token-123") == "my-t****"

    def test_non_sensitive_unchanged(self):
        assert _mask_sensitive("host", "localhost") == "localhost"
        assert _mask_sensitive("db_path", "cogito.db") == "cogito.db"


class TestSaveDefault:
    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            c = Config()
            c.save_default(path)

            # File exists and can be loaded
            loaded = Config.load(path)
            assert loaded.workspace_path == c.workspace_path
            assert loaded.storage.db_path == c.storage.db_path
            assert loaded.storage.enable_wal == c.storage.enable_wal

    def test_save_contains_expected_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            Config().save_default(path)
            content = open(path, encoding="utf-8").read()
            assert "[storage]" in content
            assert "[runtime]" in content
            assert "[interaction]" in content
            assert "[worker]" in content
            assert "workspace_path" in content

    def test_save_default_has_api_key_example(self):
        """默认配置包含 API Key 填写提示。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            Config().save_default(path)
            content = open(path, encoding="utf-8").read()
            # 现在包含注释示例，让用户知道在哪填写 API Key
            assert "api_key" in content.lower()
            assert "sk-your-key" in content


class TestProactiveConfig:
    """[proactive] 节的严格校验 + 默认值。"""

    def test_proactive_defaults(self):
        """无 [proactive] 节时 ProactiveConfig 取安全默认值：disabled + dry_run。"""
        c = Config.load("/nonexistent/path/config.toml")
        assert c.capability.proactive.enabled is False
        assert c.capability.proactive.dry_run is True
        assert c.capability.proactive.default_principal_id == "owner"
        assert c.capability.proactive.quiet_hours.enabled is True

    def test_proactive_parses_custom(self):
        """自定义 [proactive] 节正确解析字段值。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False,
                                         encoding="utf-8") as f:
            f.write(
                '[proactive]\n'
                'enabled = true\n'
                'dry_run = false\n'
                'minimum_relevance = 0.7\n'
                'max_pushes_per_day = 20\n'
                'quiet_hours.enabled = false\n'
                'quiet_hours.start = "22:00"\n'
            )
            tmp = f.name
        try:
            c = Config.load(tmp)
            assert c.capability.proactive.enabled is True
            assert c.capability.proactive.dry_run is False
            assert c.capability.proactive.minimum_relevance == 0.7
            assert c.capability.proactive.max_pushes_per_day == 20
            assert c.capability.proactive.quiet_hours.enabled is False
            assert c.capability.proactive.quiet_hours.start == "22:00"
        finally:
            os.unlink(tmp)

    def test_proactive_unknown_field_raises(self):
        """[proactive] 节未知字段报错并列出字段名。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False,
                                         encoding="utf-8") as f:
            f.write('[proactive]\nexperimental_abc = true\n')
            tmp = f.name
        try:
            with pytest.raises(ConfigError, match=r"unknown fields: experimental_abc"):
                Config.load(tmp)
        finally:
            os.unlink(tmp)

    def test_proactive_quiet_hours_unknown_field_raises(self):
        """[proactive.quiet_hours] 节未知字段报错。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False,
                                         encoding="utf-8") as f:
            f.write('[proactive.quiet_hours]\noffset_minutes = 5\n')
            tmp = f.name
        try:
            with pytest.raises(ConfigError, match=r"unknown fields: offset_minutes"):
                Config.load(tmp)
        finally:
            os.unlink(tmp)

    def test_proactive_repr_default_values(self):
        """repr 不泄露任何 secret（proactive 仅断言默认 dry_run 在 repr 内）。"""
        c = Config()
        # repr(Config) 不含 capability（刻意精简）；改为显式 repr proactive
        r = repr(c.capability.proactive)
        assert "dry_run=True" in r
        assert "enabled=False" in r


class TestPluginConfig:
    def test_top_level_plugins_alias_parses_runtime_policy(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8",
        ) as f:
            f.write(
                '[plugins]\n'
                'enabled = true\n'
                'auto_start = true\n'
                'project_paths = [".cogito/plugins"]\n'
                'granted_permissions = ["filesystem.read"]\n'
            )
            tmp = f.name
        try:
            config = Config.load(tmp)
            assert config.capability.plugins.enabled is True
            assert config.capability.plugins.auto_start is True
            assert config.capability.plugins.project_paths == [".cogito/plugins"]
            assert config.capability.plugins.granted_permissions == ["filesystem.read"]
        finally:
            os.unlink(tmp)
