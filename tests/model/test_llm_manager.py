"""Tests for LLMManager — 路由与 Provider 工厂。"""

from __future__ import annotations

import pytest

from cogito.config import ModelConfig, RoleConfig
from cogito.model.anthropic_provider import AnthropicProvider
from cogito.model.echo_provider import EchoModelProvider
from cogito.model.llm_manager import LLMManager, create_provider
from cogito.model.openai_compat import OpenAICompatProvider
from cogito.model.router import RouterError
from cogito.model.stub_provider import StubModelProvider


def _multi_cfg() -> ModelConfig:
    return ModelConfig._from_raw({
        "providers": {
            "openai": {
                "provider": "openai_compat",
                "api_key": "sk-openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o",
            },
            "anthropic": {
                "provider": "anthropic",
                "api_key": "sk-ant",
                "model": "claude-sonnet-4-20250514",
            },
        },
        "roles": {
            "main": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            "fast": {"provider": "openai", "model": "gpt-4o-mini"},
            "vlm": {"provider": "openai", "model": "gpt-4o"},
        },
    })


def _single_cfg() -> ModelConfig:
    return ModelConfig._from_raw({
        "provider": "openai_compat",
        "main": {"model": "deepseek-chat", "api_key": "sk-x", "base_url": "https://api.deepseek.com/v1"},
    })


class TestLLMManagerBuild:
    def test_multi_role_routing(self):
        mgr = LLMManager.build(_multi_cfg())
        assert isinstance(mgr.get("main"), AnthropicProvider)
        assert isinstance(mgr.get("fast"), OpenAICompatProvider)
        assert isinstance(mgr.get("vlm"), OpenAICompatProvider)
        assert mgr.router._role_map == {
            "main": "anthropic", "fast": "openai", "vlm": "openai",
        }

    def test_shared_provider_key_reuses_instance(self):
        # fast 与 vlm 指向同一 openai provider_key，应复用同一实例
        mgr = LLMManager.build(_multi_cfg())
        assert mgr.get("fast") is mgr.get("vlm")

    def test_single_provider_fallback(self):
        mgr = LLMManager.build(_single_cfg())
        assert isinstance(mgr.get("main"), OpenAICompatProvider)
        # 未配置的角色退化到 main
        assert mgr.get("memory_extractor") is mgr.get("main")

    def test_from_provider(self):
        stub = StubModelProvider()
        mgr = LLMManager.from_provider(stub)
        assert mgr.get("main") is stub
        assert mgr.get("summary") is stub

    def test_unknown_role_falls_back_to_main(self):
        # 即使 roles 中未配置 main，manager 也会注入 fallback main，
        # 因此未知角色退化到 main 而非抛错（启动鲁棒性）。
        cfg = ModelConfig._from_raw({
            "roles": {"fast": {"provider": "openai", "model": "gpt-4o-mini"}},
        })
        mgr = LLMManager.build(cfg)
        assert isinstance(mgr.get("nonexistent"), StubModelProvider)
        assert isinstance(mgr.get("fast"), StubModelProvider)  # openai 未配置完整 → stub

    def test_roles_view(self):
        mgr = LLMManager.build(_multi_cfg())
        assert set(mgr.roles) == {"main", "fast", "vlm"}


class TestCreateProvider:
    def test_echo_branch(self):
        cfg = ModelConfig._from_raw({"provider": "echo"})
        prov = create_provider(cfg.main, default_adapter=cfg.provider)
        assert isinstance(prov, EchoModelProvider)

    def test_stub_when_unconfigured(self):
        cfg = ModelConfig._from_raw({})
        prov = create_provider(cfg.main, default_adapter="openai_compat")
        assert isinstance(prov, StubModelProvider)

    def test_anthropic_branch(self):
        cfg = _multi_cfg()
        prov = create_provider(cfg.providers["anthropic"])
        assert isinstance(prov, AnthropicProvider)
        assert prov._base_url == "https://api.anthropic.com"

    def test_openai_compat_branch(self):
        cfg = _single_cfg()
        prov = create_provider(cfg.main, default_adapter=cfg.provider)
        assert isinstance(prov, OpenAICompatProvider)


class TestConfigResolveRole:
    def test_resolve_role_overrides_model(self):
        cfg = _multi_cfg()
        key, ep = cfg.resolve_role("main")
        assert key == "anthropic"
        assert ep.model == "claude-sonnet-4-20250514"
        # 不应修改共享的 providers 中的原始 endpoint
        assert cfg.providers["anthropic"].model == "claude-sonnet-4-20250514"

    def test_resolve_role_legacy_fallback(self):
        cfg = _single_cfg()
        key, ep = cfg.resolve_role("memory_extractor")
        assert key == "main"
        assert ep.model == "deepseek-chat"
