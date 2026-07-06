import pytest

from cogito.llm.protocol import ChatProvider
from cogito.llm.registry import ModelRegistry, UnknownModelError


class TestModelRegistry:
    def test_get_known_model(self):
        provider = _make_provider()
        registry = ModelRegistry({"main": provider})

        result = registry.get("main")
        assert result is provider

    def test_get_unknown_model(self):
        registry = ModelRegistry({})
        with pytest.raises(UnknownModelError, match="unknown model profile"):
            registry.get("nonexistent")

    def test_get_unknown_model_is_key_error(self):
        registry = ModelRegistry({})
        with pytest.raises(KeyError):
            registry.get("nonexistent")

    @pytest.mark.asyncio
    async def test_close_calls_all_providers(self):
        provider1 = _make_provider()
        provider2 = _make_provider()
        registry = ModelRegistry({"a": provider1, "b": provider2})

        await registry.close()

        provider1.close.assert_awaited_once()
        provider2.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_deduplicates_same_instance(self):
        provider = _make_provider()
        registry = ModelRegistry({"a": provider, "b": provider})

        await registry.close()

        provider.close.assert_awaited_once()


def _make_provider():
    import asyncio
    from unittest.mock import AsyncMock

    mock = AsyncMock(spec=ChatProvider)
    mock.complete = AsyncMock()
    mock.stream.__name__ = "stream"
    mock.close = AsyncMock()
    return mock
