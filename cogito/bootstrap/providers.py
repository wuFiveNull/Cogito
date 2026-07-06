# cogito/bootstrap/providers.py

from __future__ import annotations

import os

from openai import AsyncOpenAI

from cogito.config.errors import ConfigError
from cogito.config.schema import AppConfig, ProviderConfig
from cogito.llm import (
    Embedder,
    EmbeddingProfile,
    ModelCapabilities,
    ModelProfile,
)
from cogito.llm.adapters import ADAPTER_FACTORIES
from cogito.llm.backend import ChatBackend
from cogito.llm.registry import ModelRegistry
from cogito.llm.service import LLMService


def resolve_api_key(
    provider_name: str,
    config: ProviderConfig,
) -> str:
    # 开发阶段：允许 api_key 明文存放
    if config.api_key:
        return config.api_key

    value = os.getenv(config.api_key_env)

    if not value:
        raise ConfigError(
            f"missing API key for provider {provider_name!r}; "
            f"set {config.api_key_env} env var "
            f"or provider.api_key in config"
        )

    return value


def build_capabilities(values: set[str]) -> ModelCapabilities:
    return ModelCapabilities(
        text="text" in values,
        tools="tools" in values,
        vision="vision" in values,
        thinking="thinking" in values,
        streaming="streaming" in values,
        embedding="embedding" in values,
    )


def build_llm_service(config: AppConfig) -> LLMService:
    models: dict[str, ChatBackend] = {}

    for model_name, model_config in config.llm.models.items():
        if "embedding" in model_config.capabilities:
            continue

        provider_config = config.llm.providers[model_config.provider]

        try:
            adapter_factory = ADAPTER_FACTORIES[provider_config.adapter]
        except KeyError as exc:
            raise ConfigError(
                f"unknown provider adapter: {provider_config.adapter}"
            ) from exc

        api_key = resolve_api_key(model_config.provider, provider_config)

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=provider_config.base_url,
            default_headers=provider_config.default_headers or None,
            max_retries=0,
        )

        profile = ModelProfile(
            name=model_name,
            provider=model_config.provider,
            model=model_config.model,
            capabilities=build_capabilities(model_config.capabilities),
            max_output_tokens=model_config.max_output_tokens,
            default_extra_body=dict(model_config.extra_body),
        )

        models[model_name] = ChatBackend(
            provider_name=model_config.provider,
            client=client,
            adapter=adapter_factory(),
            profile=profile,
            request_timeout_s=provider_config.request_timeout_s,
            stream_idle_timeout_s=provider_config.stream_idle_timeout_s,
            max_retries=provider_config.max_retries,
            retry_base_delay_s=provider_config.retry_base_delay_s,
            retry_max_delay_s=provider_config.retry_max_delay_s,
        )

    registry = ModelRegistry(models)

    return LLMService(
        registry=registry,
        routes=config.llm.routes,
    )


def build_embedder(config: AppConfig) -> Embedder | None:
    """Build an embedder if the config has an embedding model.

    This is a placeholder — actual embedding implementation will be added
    when embedding support is fully implemented.
    """
    for model_config in config.llm.models.values():
        if "embedding" in model_config.capabilities:
            provider_config = config.llm.providers[model_config.provider]
            api_key = resolve_api_key(model_config.provider, provider_config)

            profile = EmbeddingProfile(
                provider=model_config.provider,
                model=model_config.model,
                base_url=provider_config.base_url,
                api_key=api_key,
                dimensions=model_config.dimensions,
                max_batch_size=model_config.max_batch_size,
            )

            # Return a simple embedder for now
            return _OpenAIEmbedder(profile)

    return None


def load_system_prompt(config: AppConfig) -> str:
    path = config.resolve_path(config.agent.system_prompt_file)

    if not path.is_file():
        raise ConfigError(f"system prompt file not found: {path}")

    return path.read_text(encoding="utf-8").strip()


class _OpenAIEmbedder:
    """Minimal OpenAI API-compatible embedder."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        self._profile = profile
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=profile.api_key,
            base_url=profile.base_url,
            max_retries=0,
        )

    async def embed(self, text: str) -> list[float]:
        resp = await self._client.embeddings.create(
            model=self._profile.model,
            input=text,
        )
        return resp.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        results: list[list[float]] = []
        batch_size = self._profile.max_batch_size

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = await self._client.embeddings.create(
                model=self._profile.model,
                input=batch,
            )
            results.extend([item.embedding for item in resp.data])
            await asyncio.sleep(0)

        return results

    async def close(self) -> None:
        await self._client.close()
