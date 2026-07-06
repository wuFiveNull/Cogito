"""Tests for bootstrap/providers.py"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogito.bootstrap.providers import build_capabilities, resolve_api_key
from cogito.config.errors import ConfigError
from cogito.config.schema import ProviderConfig
from cogito.llm.capabilities import ModelCapabilities


class TestResolveAPIKey:
    def test_key_found(self):
        config = ProviderConfig(
            adapter="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key_env="DEEPSEEK_API_KEY",
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-key"}, clear=False):
            key = resolve_api_key("deepseek", config)
            assert key == "sk-test-key"

    def test_key_missing(self):
        config = ProviderConfig(
            adapter="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key_env="MISSING_KEY",
        )
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigError, match="missing API key"):
                resolve_api_key("deepseek", config)


class TestBuildCapabilities:
    def test_text_only(self):
        caps = build_capabilities({"text"})
        assert caps.text is True
        assert caps.tools is False
        assert caps.vision is False
        assert caps.thinking is False
        assert caps.streaming is False  # "streaming" not in set
        assert caps.embedding is False

    def test_all_capabilities(self):
        caps = build_capabilities({"text", "tools", "vision", "thinking", "streaming", "embedding"})
        assert all([caps.text, caps.tools, caps.vision, caps.thinking, caps.streaming, caps.embedding])

    def test_empty_set(self):
        caps = build_capabilities(set())
        assert caps.text is False
        assert caps.streaming is False
        assert caps.embedding is False


class TestBuildLLMService:
    @pytest.mark.asyncio
    async def test_build_with_mock_config(self):
        """Test that build_llm_service creates an LLMService with correct routes."""
        from cogito.bootstrap.providers import build_llm_service
        from cogito.config.schema import AppConfig, LLMConfig, ModelConfig, ProviderConfig

        config = AppConfig(
            llm=LLMConfig(
                providers={
                    "test_provider": ProviderConfig(
                        adapter="openai_compatible",
                        base_url="https://api.example.com/v1",
                        api_key_env="TEST_API_KEY",
                    ),
                },
                models={
                    "test_model": ModelConfig(
                        provider="test_provider",
                        model="test-model",
                        capabilities={"text", "streaming"},
                    ),
                },
                routes={"main": "test_model"},
            ),
        )

        with patch.dict(os.environ, {"TEST_API_KEY": "sk-test"}, clear=False):
            with patch("cogito.bootstrap.providers.AsyncOpenAI") as mock_client:
                mock_instance = MagicMock()
                mock_instance.close = AsyncMock()
                mock_client.return_value = mock_instance
                service = build_llm_service(config)

                assert service.provider_for("main") is not None
                await service.close()

    def test_embedding_model_skipped(self):
        """Models with embedding capability should be skipped by build_llm_service."""
        from cogito.bootstrap.providers import build_llm_service
        from cogito.config.schema import AppConfig, LLMConfig, ModelConfig, ProviderConfig
        from cogito.llm.registry import UnknownModelError

        config = AppConfig(
            llm=LLMConfig(
                providers={
                    "test_provider": ProviderConfig(
                        adapter="openai_compatible",
                        base_url="https://api.example.com/v1",
                        api_key_env="TEST_API_KEY",
                    ),
                },
                models={
                    "text_model": ModelConfig(
                        provider="test_provider",
                        model="text-model",
                        capabilities={"text", "streaming"},
                    ),
                    "emb_model": ModelConfig(
                        provider="test_provider",
                        model="emb-model",
                        capabilities={"embedding"},
                    ),
                },
                routes={"main": "text_model"},
            ),
        )

        with patch.dict(os.environ, {"TEST_API_KEY": "sk-test"}, clear=False):
            with patch("cogito.bootstrap.providers.AsyncOpenAI") as mock_client:
                mock_client.return_value = MagicMock()
                service = build_llm_service(config)

                # "text_model" should be registered
                assert service.provider_for("main") is not None

                # "emb_model" was skipped — only text_model is in the registry
                registry = service._registry
                assert len(registry._models) == 1
                with pytest.raises(UnknownModelError):
                    registry.get("emb_model")
