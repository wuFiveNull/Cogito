import pytest

from pydantic import ValidationError

from cogito.config.schema import (
    AppConfig,
    LLMConfig,
    ModelConfig,
    ProviderConfig,
)


class TestProviderConfig:
    def test_valid(self):
        config = ProviderConfig(
            adapter="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key_env="DEEPSEEK_API_KEY",
        )
        assert config.adapter == "deepseek"
        assert config.base_url == "https://api.deepseek.com/v1"
        assert config.max_retries == 2

    def test_invalid_adapter(self):
        with pytest.raises(ValidationError):
            ProviderConfig(
                adapter="unknown",
                base_url="https://api.example.com",
                api_key_env="KEY",
            )

    def test_default_headers(self):
        config = ProviderConfig(
            adapter="openai",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
        )
        assert config.default_headers == {}


class TestModelConfig:
    def test_minimal(self):
        config = ModelConfig(provider="deepseek", model="deepseek-chat")
        assert config.max_output_tokens == 4096
        assert "text" in config.capabilities
        assert "streaming" in config.capabilities

    def test_with_capabilities(self):
        config = ModelConfig(
            provider="deepseek",
            model="deepseek-chat",
            capabilities={"text", "tools", "thinking", "streaming"},
        )
        assert "tools" in config.capabilities

    def test_embedding_defaults(self):
        config = ModelConfig(
            provider="dashscope",
            model="text-embedding-v3",
            capabilities={"embedding"},
            dimensions=1024,
        )
        assert config.dimensions == 1024
        assert config.max_batch_size == 10


class TestAppConfig:
    def test_minimal(self):
        config = AppConfig(
            llm=LLMConfig(
                providers={
                    "deepseek": ProviderConfig(
                        adapter="deepseek",
                        base_url="https://api.deepseek.com/v1",
                        api_key_env="DEEPSEEK_API_KEY",
                    ),
                },
                models={
                    "main": ModelConfig(provider="deepseek", model="deepseek-chat"),
                },
                routes={"main": "main"},
            )
        )
        assert config.app.name == "cogito"
        assert config.loop.max_concurrent_sessions == 4

    def test_resolve_path_absolute(self):
        from pathlib import Path

        config = AppConfig(
            llm=LLMConfig(
                providers={},
                models={},
                routes={},
            ),
            project_dir=Path("/project"),
        )
        resolved = config.resolve_path(Path("/absolute/path.txt"))
        assert resolved == Path("/absolute/path.txt").resolve()

    def test_resolve_path_relative(self):
        from pathlib import Path

        config = AppConfig(
            llm=LLMConfig(
                providers={},
                models={},
                routes={},
            ),
            project_dir=Path("/project"),
        )
        resolved = config.resolve_path(Path("relative/path.txt"))
        assert resolved == Path("/project/relative/path.txt").resolve()
