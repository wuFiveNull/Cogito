"""Tests for config loader."""

import os
from pathlib import Path

import pytest

from cogito.config.errors import ConfigError
from cogito.config.loader import (
    apply_environment_overrides,
    expand_env_in_value,
    find_config_path,
    load_config,
    set_nested,
)


class TestExpandEnvInValue:
    def test_no_env_var(self):
        result = expand_env_in_value("hello world")
        assert result == "hello world"

    def test_env_var_expanded(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key")
        result = expand_env_in_value("${DEEPSEEK_API_KEY}")
        assert result == "sk-test-key"

    def test_missing_env_var(self):
        with pytest.raises(ConfigError, match="required environment variable"):
            expand_env_in_value("${MISSING_VAR}")

    def test_in_string(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "abc123")
        result = expand_env_in_value("prefix-${TOKEN}-suffix")
        assert result == "prefix-abc123-suffix"

    def test_in_dict(self, monkeypatch):
        monkeypatch.setenv("KEY", "value")
        result = expand_env_in_value({"nested": "${KEY}"})
        assert result == {"nested": "value"}

    def test_in_list(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        result = expand_env_in_value(["${HOST}", "port"])
        assert result == ["localhost", "port"]


class TestApplyEnvironmentOverrides:
    def test_simple_override(self, monkeypatch):
        monkeypatch.setenv("COGITO__APP__ENVIRONMENT", '"staging"')
        data = {"app": {"environment": "development"}}

        result = apply_environment_overrides(data)
        assert result["app"]["environment"] == "staging"

    def test_nested_path(self, monkeypatch):
        monkeypatch.setenv("COGITO__LLM__MODELS__MAIN__MAX_OUTPUT_TOKENS", "8192")
        data = {
            "llm": {
                "models": {
                    "main": {"max_output_tokens": 4096},
                },
            },
        }

        result = apply_environment_overrides(data)
        assert result["llm"]["models"]["main"]["max_output_tokens"] == 8192

    def test_boolean_override(self, monkeypatch):
        monkeypatch.setenv("COGITO__AGENT__SHOW_THINKING", "true")
        data = {"agent": {"show_thinking": False}}

        result = apply_environment_overrides(data)
        assert result["agent"]["show_thinking"] is True

    def test_null_override(self, monkeypatch):
        monkeypatch.setenv("COGITO__SOME__VALUE", "null")
        data = {"some": {"value": "exists"}}

        result = apply_environment_overrides(data)
        assert result["some"]["value"] is None

    def test_no_prefix_vars_ignored(self, monkeypatch):
        monkeypatch.setenv("OTHER__VAR", "value")
        data = {"key": "original"}

        result = apply_environment_overrides(data)
        assert result == data

    def test_original_data_unchanged(self, monkeypatch):
        monkeypatch.setenv("COGITO__APP__ENVIRONMENT", '"production"')
        data = {"app": {"environment": "development"}}

        result = apply_environment_overrides(data)
        assert result["app"]["environment"] == "production"
        assert data["app"]["environment"] == "development"


class TestFindConfigPath:
    def test_explicit_path_found(self, tmp_path):
        config_file = tmp_path / "myconfig.toml"
        config_file.write_text("")

        result = find_config_path(str(config_file))
        assert result == config_file.resolve()

    def test_explicit_path_not_found(self):
        with pytest.raises(ConfigError, match="config file not found"):
            find_config_path("/nonexistent/config.toml")

    def test_env_var_path(self, monkeypatch, tmp_path):
        config_file = tmp_path / "from_env.toml"
        config_file.write_text("")
        monkeypatch.setenv("COGITO_CONFIG", str(config_file))

        result = find_config_path(None)
        assert result == config_file.resolve()

    def test_env_var_path_not_found(self, monkeypatch):
        monkeypatch.setenv("COGITO_CONFIG", "/nonexistent/file.toml")
        with pytest.raises(ConfigError, match="COGITO_CONFIG points to"):
            find_config_path(None)

    def test_no_config_found(self, monkeypatch, tmp_path):
        monkeypatch.delenv("COGITO_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ConfigError, match="configuration file not found"):
            find_config_path(None)


class TestSetNested:
    def test_simple(self):
        target = {}
        set_nested(target, ["a", "b", "c"], "value")
        assert target == {"a": {"b": {"c": "value"}}}

    def test_overwrite(self):
        target = {"a": {"b": 1}}
        set_nested(target, ["a", "b"], 2)
        assert target["a"]["b"] == 2


class TestLoadConfig:
    def test_successful_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test2")

        config_file = tmp_path / "config" / "config.toml"
        config_file.parent.mkdir(exist_ok=True)
        config_file.write_text("""
[app]
name = "cogito"
environment = "test"

[agent]
show_thinking = true

[loop]
max_concurrent_sessions = 2

[storage]
sqlite_path = "data/test.db"

[delivery]
channel_queue_size = 50

[llm.providers.deepseek]
adapter = "deepseek"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"

[llm.providers.dashscope]
adapter = "dashscope"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"

[llm.models.main]
provider = "deepseek"
model = "deepseek-chat"
max_output_tokens = 8192
capabilities = ["text", "tools", "thinking", "streaming"]

[llm.models.light]
provider = "dashscope"
model = "qwen-plus"
capabilities = ["text", "tools", "streaming"]

[llm.routes]
main = "main"
light = "light"
""")

        result = load_config(str(config_file))
        assert result.app.name == "cogito"
        assert result.app.environment == "test"
        assert result.agent.show_thinking is True
        assert result.loop.max_concurrent_sessions == 2
        assert len(result.llm.providers) == 2
        assert len(result.llm.models) == 2
        assert result.llm.routes["main"] == "main"

    def test_invalid_toml(self, tmp_path):
        config_file = tmp_path / "bad.toml"
        config_file.write_text("{{ bad toml }")

        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(str(config_file))

    def test_invalid_reference_model_to_provider(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "sk-xxx")
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[llm.providers.deepseek]
adapter = "deepseek"
base_url = "https://api.deepseek.com/v1"
api_key_env = "KEY"

[llm.models.main]
provider = "nonexistent"
model = "deepseek-chat"

[llm.routes]
main = "main"
""")

        with pytest.raises(ConfigError, match="unknown provider"):
            load_config(str(config_file))

    def test_invalid_reference_route_to_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "sk-xxx")
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[llm.providers.deepseek]
adapter = "deepseek"
base_url = "https://api.deepseek.com/v1"
api_key_env = "KEY"

[llm.models.main]
provider = "deepseek"
model = "deepseek-chat"

[llm.routes]
main = "nonexistent"
""")

        with pytest.raises(ConfigError, match="unknown model"):
            load_config(str(config_file))
