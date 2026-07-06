# cogito/llm/protocol.py

from typing import Protocol, runtime_checkable
from collections.abc import AsyncIterator

from .request import ChatRequest
from .response import LLMResponse
from .stream import LLMStreamEvent


@runtime_checkable
class ChatProvider(Protocol):
    """Protocol for LLM chat providers."""

    async def complete(
        self,
        request: ChatRequest,
    ) -> LLMResponse:
        ...

    def stream(
        self,
        request: ChatRequest,
    ) -> AsyncIterator[LLMStreamEvent]:
        ...

    async def close(self) -> None:
        ...
