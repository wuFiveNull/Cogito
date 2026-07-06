"""Tests for strict configuration model."""

import os
import tempfile

import pytest

from cogito.config import Config, _mask_sensitive


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
        assert "RuntimeConfig()" in r
        assert "InteractionConfig()" in r

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
            with pytest.raises(ValueError, match="Unknown config key/section"):
                Config.load(tmp)
        finally:
            os.unlink(tmp)

    def test_unknown_field_in_storage_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[storage]\nunknown_field = 1\n')
            tmp = f.name
        try:
            with pytest.raises(ValueError, match="Unknown config fields"):
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
            assert "workspace_path" in content

    def test_save_default_has_no_api_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            Config().save_default(path)
            content = open(path, encoding="utf-8").read()
            assert "api_key" not in content.lower()
