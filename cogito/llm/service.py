# cogito/llm/service.py

from .protocol import ChatProvider
from .registry import ModelRegistry
from .request import ChatRequest
from .response import LLMResponse


class UnknownModelRoleError(KeyError):
    pass


class LLMService:
    def __init__(
        self,
        *,
        registry: ModelRegistry,
        routes: dict[str, str],
    ):
        self._registry = registry
        self._routes = dict(routes)

    def provider_for(
        self,
        role: str,
    ) -> ChatProvider:
        try:
            model_name = self._routes[role]
        except KeyError as exc:
            raise UnknownModelRoleError(
                f"unknown LLM role: {role}"
            ) from exc

        return self._registry.get(model_name)

    async def complete(
        self,
        role: str,
        request: ChatRequest,
    ) -> LLMResponse:
        provider = self.provider_for(role)
        return await provider.complete(request)

    def stream(
        self,
        role: str,
        request: ChatRequest,
    ):
        provider = self.provider_for(role)
        return provider.stream(request)

    async def close(self) -> None:
        await self._registry.close()
