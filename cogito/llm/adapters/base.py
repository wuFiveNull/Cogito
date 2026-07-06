# cogito/llm/adapters/base.py

from abc import ABC, abstractmethod
from typing import Any

from cogito.llm.capabilities import ModelProfile
from cogito.llm.errors import LLMError
from cogito.llm.request import ChatRequest
from cogito.llm.response import LLMResponse
from cogito.llm.stream import LLMStreamEvent


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    def build_request(
        self,
        profile: ModelProfile,
        request: ChatRequest,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def parse_response(
        self,
        raw_response: Any,
        profile: ModelProfile,
    ) -> LLMResponse:
        ...

    @abstractmethod
    def parse_stream_chunk(
        self,
        chunk: Any,
    ) -> tuple[LLMStreamEvent, ...]:
        ...

    @abstractmethod
    def map_error(
        self,
        exc: Exception,
    ) -> LLMError:
        ...
