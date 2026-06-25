import pytest

from cogito.llm.protocol import ChatProvider
from cogito.llm.registry import ModelRegistry
from cogito.llm.request import ChatMessage, ChatRequest
from cogito.llm.response import LLMResponse
from cogito.llm.service import LLMService, UnknownModelRoleError


class TestLLMService:
    def test_provider_for_known_role(self):
        provider = _make_provider()
        registry = ModelRegistry({"main": provider})
        service = LLMService(registry=registry, routes={"main": "main"})

        result = service.provider_for("main")
        assert result is provider

    def test_provider_for_unknown_role(self):
        registry = ModelRegistry({})
        service = LLMService(registry=registry, routes={})

        with pytest.raises(UnknownModelRoleError, match="unknown LLM role"):
            service.provider_for("nonexistent")

    def test_provider_for_unknown_role_is_key_error(self):
        registry = ModelRegistry({})
        service = LLMService(registry=registry, routes={})

        with pytest.raises(KeyError):
            service.provider_for("nonexistent")

    @pytest.mark.asyncio
    async def test_complete_routes_correctly(self):
        main_provider = _make_provider()
        main_provider.complete.return_value = LLMResponse(content="main response")

        light_provider = _make_provider()
        light_provider.complete.return_value = LLMResponse(content="light response")

        registry = ModelRegistry({"main": main_provider, "light": light_provider})
        service = LLMService(registry=registry, routes={"main": "main", "light": "light"})

        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))

        result = await service.complete("light", request)
        assert result.content == "light response"

        result = await service.complete("main", request)
        assert result.content == "main response"

    @pytest.mark.asyncio
    async def test_close_closes_registry(self):
        provider = _make_provider()
        registry = ModelRegistry({"main": provider})
        service = LLMService(registry=registry, routes={"main": "main"})

        await service.close()
        provider.close.assert_awaited_once()


def _make_provider():
    from unittest.mock import AsyncMock
    import asyncio

    async def _empty_stream(_request):
        return
        yield  # make it a generator

    mock = AsyncMock(spec=ChatProvider)
    mock.complete = AsyncMock()
    mock.stream = AsyncMock()
    mock.close = AsyncMock()
    return mock
