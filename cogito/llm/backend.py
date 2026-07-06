# cogito/llm/backend.py

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from cogito.llm.capabilities import ModelProfile
from cogito.llm.errors import LLMError, LLMTimeoutError
from cogito.llm.protocol import ChatProvider
from cogito.llm.request import ChatRequest
from cogito.llm.response import LLMResponse
from cogito.llm.stream import LLMStreamEvent

from .adapters.base import ProviderAdapter


class ChatBackend(ChatProvider):
    def __init__(
        self,
        *,
        provider_name: str,
        client: AsyncOpenAI,
        adapter: ProviderAdapter,
        profile: ModelProfile,
        request_timeout_s: float = 180.0,
        stream_idle_timeout_s: float = 90.0,
        max_retries: int = 2,
        retry_base_delay_s: float = 1.0,
        retry_max_delay_s: float = 30.0,
    ):
        self._provider_name = provider_name
        self._client = client
        self._adapter = adapter
        self._profile = profile

        self._request_timeout_s = request_timeout_s
        self._stream_idle_timeout_s = stream_idle_timeout_s

        self._max_retries = max_retries
        self._retry_base_delay_s = retry_base_delay_s
        self._retry_max_delay_s = retry_max_delay_s

    # ------------------------------------------------------------------
    # Complete (non-streaming)
    # ------------------------------------------------------------------

    async def complete(
        self,
        request: ChatRequest,
    ) -> LLMResponse:
        payload = self._adapter.build_request(
            self._profile,
            request,
            stream=False,
        )

        raw = await self._request_with_retry(payload)

        return self._adapter.parse_response(raw, self._profile)

    async def _request_with_retry(self, payload: dict):
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                async with asyncio.timeout(self._request_timeout_s):
                    return await self._client.chat.completions.create(**payload)

            except asyncio.CancelledError:
                raise

            except TimeoutError as exc:
                last_exc = exc
                error = LLMTimeoutError(
                    code="request_timeout",
                    message="LLM request timed out",
                    retryable=True,
                    provider=self._provider_name,
                )

            except Exception as exc:
                last_exc = exc
                error = self._adapter.map_error(exc)

            if not error.retryable:
                raise error from last_exc

            if attempt >= self._max_retries:
                raise error from last_exc

            delay = (
                error.retry_after
                if error.retry_after is not None
                else self._retry_delay(attempt)
            )

            await asyncio.sleep(delay)

        raise RuntimeError("unreachable")

    def _retry_delay(self, attempt: int) -> float:
        raw = min(
            self._retry_base_delay_s * (2**attempt),
            self._retry_max_delay_s,
        )
        jitter = raw * 0.2
        return max(0.0, raw + random.uniform(-jitter, jitter))

    # ------------------------------------------------------------------
    # Stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        request: ChatRequest,
    ) -> AsyncIterator[LLMStreamEvent]:
        payload = self._adapter.build_request(
            self._profile,
            request,
            stream=True,
        )

        try:
            raw_stream = await self._client.chat.completions.create(**payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mapped = self._adapter.map_error(exc)
            raise mapped from exc

        first_delta_received = False

        try:
            async for chunk in raw_stream:
                events = self._adapter.parse_stream_chunk(chunk)
                if events:
                    first_delta_received = True
                    for event in events:
                        yield event
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if not first_delta_received:
                raise LLMTimeoutError(
                    code="stream_timeout",
                    message="Stream timed out before first delta",
                    retryable=True,
                    provider=self._provider_name,
                ) from None
            raise
        except Exception as exc:
            mapped = self._adapter.map_error(exc)
            raise mapped from exc

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._client.close()
