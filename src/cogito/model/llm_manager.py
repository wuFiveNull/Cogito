"""LLMManager —— 模型类别路由门面。

聚合多个 Provider 实例，按配置构建 ModelRouter，提供：
- get(role) -> ModelProvider：按类别（main/fast/vlm）获取主 Provider
- router：底层路由器，供现有调用方（AgentLoop 等）继续使用
    router.generate(request, model_role=role)

MODEL-ADAPTER / 8. 重试与 Fallback：路由器负责，本层只做编排。
CONFIG-PROFILES / 1：配置驱动，roles 未配置时退化到单 Provider 行为。
"""

from __future__ import annotations

import logging

from cogito.config import ModelConfig, ModelEndpointConfig
from cogito.model.provider import ModelProvider
from cogito.model.router import ModelRouter, RouterError

_LOGGER = logging.getLogger(__name__)


def create_provider(
    endpoint: ModelEndpointConfig,
    default_adapter: str = "openai_compat",
) -> ModelProvider:
    """根据 endpoint 配置创建具体的 Provider 实例。

    endpoint.provider 指定适配器类型（openai_compat / anthropic / echo）；
    未指定时使用 default_adapter。未配置完整时返回 Stub，避免误调真实模型。
    """
    adapter = endpoint.provider or default_adapter

    if adapter == "echo":
        from cogito.model.echo_provider import EchoModelProvider
        return EchoModelProvider(model_id=endpoint.model or "echo-model")

    # 按适配器补全默认 base_url（Anthropic 有官方默认；OpenAI 兼容层无通用默认）
    effective_base_url = endpoint.base_url
    if not effective_base_url and adapter == "anthropic":
        effective_base_url = "https://api.anthropic.com"

    if not (endpoint.model and endpoint.api_key and effective_base_url):
        from cogito.model.stub_provider import StubModelProvider
        return StubModelProvider()

    if adapter == "anthropic":
        from cogito.model.anthropic_provider import AnthropicProvider
        return AnthropicProvider(
            model=endpoint.model,
            api_key=endpoint.api_key,
            base_url=effective_base_url,
            timeout_seconds=endpoint.timeout_seconds,
        )

    # 默认：OpenAI 兼容（OpenAI / DeepSeek / vLLM / LiteLLM / ...）
    from cogito.model.openai_compat import OpenAICompatProvider
    return OpenAICompatProvider(
        model=endpoint.model,
        api_key=endpoint.api_key,
        base_url=effective_base_url,
        timeout_seconds=endpoint.timeout_seconds,
        modalities=endpoint.modalities,
    )


class LLMManager:
    """模型类别路由器。

    用法：
        manager = LLMManager.build(config.model)
        llm = manager.get("main")        # -> ModelProvider
        response = manager.router.generate(request, model_role="main")
    """

    def __init__(
        self,
        providers: dict[str, ModelProvider],
        router: ModelRouter,
        role_map: dict[str, str],
    ) -> None:
        self._providers = providers
        self._router = router
        self._role_map = role_map

    @classmethod
    def build(cls, model_cfg: ModelConfig) -> LLMManager:
        """从 ModelConfig 构建 Provider 实例与路由器。

        配置了 roles 时走多 Provider 角色路由；否则退化到单 Provider，
        所有角色指向同一个 Provider（向后兼容当前行为）。
        """
        if model_cfg.roles:
            return cls._build_multi(model_cfg)
        return cls._build_single(model_cfg)

    @classmethod
    def from_provider(cls, provider: ModelProvider) -> LLMManager:
        """从单个显式 Provider 构建（测试 / 自定义注入场景）。

        所有角色退化到该 Provider，行为与旧版单 Provider 一致。
        """
        providers = {"main": provider}
        role_map = {
            "main": "main",
            "memory_extractor": "main",
            "summary": "main",
            "query_rewriter": "main",
        }
        router = ModelRouter(providers=providers, role_map=role_map)
        return cls(providers=providers, router=router, role_map=role_map)

    @classmethod
    def _build_multi(cls, model_cfg: ModelConfig) -> LLMManager:
        providers: dict[str, ModelProvider] = {}
        role_map: dict[str, str] = {}

        for role_name, role_cfg in model_cfg.roles.items():
            provider_key, endpoint = model_cfg.resolve_role(role_name)
            # 同 provider_key 复用同一实例，避免重复创建 HTTP 客户端
            if provider_key not in providers:
                providers[provider_key] = create_provider(endpoint)
            role_map[role_name] = provider_key

        # 至少保证 main 可用
        if "main" not in role_map:
            _LOGGER.warning(
                "roles configured but 'main' missing; falling back to [model.main]",
            )
            providers["main"] = create_provider(model_cfg.main, model_cfg.provider)
            role_map["main"] = "main"

        router = ModelRouter(providers=providers, role_map=role_map)
        return cls(providers=providers, router=router, role_map=role_map)

    @classmethod
    def _build_single(cls, model_cfg: ModelConfig) -> LLMManager:
        provider = create_provider(model_cfg.main, model_cfg.provider)
        providers = {"main": provider}
        role_map = {
            "main": "main",
            "memory_extractor": "main",
            "summary": "main",
            "query_rewriter": "main",
        }
        router = ModelRouter(providers=providers, role_map=role_map)
        return cls(providers=providers, router=router, role_map=role_map)

    @property
    def router(self) -> ModelRouter:
        """底层路由器 —— 现有调用方继续使用 router.generate(request, model_role=...)。"""
        return self._router

    @property
    def roles(self) -> dict[str, str]:
        """只读的角色映射视图。"""
        return dict(self._role_map)

    def get(self, role: str) -> ModelProvider:
        """按模型类别获取主 Provider。

        适用于需要直接能力声明（capabilities()）或直接调用的场景。
        注意：绕过 router，将失去重试 / fallback / 可观测性回调。
        """
        provider_id = self._resolve_provider_id(role)
        provider = self._providers.get(provider_id)
        if provider is None:
            raise RouterError(f"Provider not found for role: {role}")
        return provider

    def _resolve_provider_id(self, role: str) -> str:
        provider_id = self._role_map.get(role)
        if provider_id is not None:
            return provider_id
        if role != "main" and "main" in self._role_map:
            _LOGGER.warning(
                "No provider configured for role '%s', falling back to 'main'",
                role,
            )
            return "main"
        raise RouterError(f"No provider configured for role: {role}")
