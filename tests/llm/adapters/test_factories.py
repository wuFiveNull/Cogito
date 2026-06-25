"""Tests for ADAPTER_FACTORIES registry."""

from cogito.llm.adapters import (
    ADAPTER_FACTORIES,
    DashScopeAdapter,
    DeepSeekAdapter,
    OpenAIAdapter,
    OpenAICompatibleAdapter,
)


class TestAdapterFactories:
    def test_openai(self):
        assert ADAPTER_FACTORIES["openai"] is OpenAIAdapter

    def test_openai_compatible(self):
        assert ADAPTER_FACTORIES["openai_compatible"] is OpenAICompatibleAdapter

    def test_deepseek(self):
        assert ADAPTER_FACTORIES["deepseek"] is DeepSeekAdapter

    def test_dashscope(self):
        assert ADAPTER_FACTORIES["dashscope"] is DashScopeAdapter

    def test_all_can_be_instantiated(self):
        for name, factory in ADAPTER_FACTORIES.items():
            instance = factory()
            assert instance.name is not None
