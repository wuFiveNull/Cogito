# cogito/llm/adapters/openai_compatible.py

from __future__ import annotations

import json
from typing import Any

from openai import (
    APIError,
    APITimeoutError,
    APIConnectionError,
    AuthenticationError,
    RateLimitError,
    BadRequestError,
    InternalServerError,
    APIResponseValidationError,
)

from cogito.llm.capabilities import ModelProfile, validate_request_capabilities
from cogito.llm.errors import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    ContentSafetyError,
    ContextLengthError,
    InvalidLLMResponseError,
    ModelCapabilityError,
)
from cogito.llm.request import (
    ChatMessage,
    ChatRequest,
    ImageContent,
    TextContent,
)
from cogito.llm.response import LLMResponse, TokenUsage, ToolCall
from cogito.llm.stream import (
    ContentDelta,
    LLMStreamEvent,
    StreamCompleted,
    ThinkingDelta,
    ToolCallDelta,
    UsageDelta,
)

from .base import ProviderAdapter


_OPENAI_FINISH_REASON_MAP: dict[str, str | None] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "content_filter": "content_filter",
    "function_call": "tool_calls",
}


class OpenAICompatibleAdapter(ProviderAdapter):
    name = "openai_compatible"

    # ------------------------------------------------------------------
    # Build request
    # ------------------------------------------------------------------

    def build_request(
        self,
        profile: ModelProfile,
        request: ChatRequest,
        *,
        stream: bool,
    ) -> dict:
        validate_request_capabilities(profile, request)

        payload: dict[str, Any] = {
            "model": profile.model,
            "messages": [
                self._serialize_message(message)
                for message in request.messages
            ],
            "stream": stream,
        }

        max_tokens = request.max_output_tokens or profile.max_output_tokens
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        if request.temperature is not None:
            payload["temperature"] = request.temperature

        if request.top_p is not None:
            payload["top_p"] = request.top_p

        if request.stop:
            payload["stop"] = list(request.stop)

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                    },
                }
                for tool in request.tools
            ]

            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice

        extra_body = dict(profile.default_extra_body)
        if extra_body:
            payload["extra_body"] = extra_body

        if stream:
            payload["stream_options"] = {"include_usage": True}

        return payload

    # ------------------------------------------------------------------
    # Serialize messages
    # ------------------------------------------------------------------

    def _serialize_message(self, message: ChatMessage) -> dict[str, Any]:
        serialized: dict[str, Any] = {"role": message.role}

        if message.content is not None:
            serialized["content"] = self._serialize_content(message.content)
        else:
            serialized["content"] = None

        if message.name is not None:
            serialized["name"] = message.name

        if message.tool_call_id is not None:
            serialized["tool_call_id"] = message.tool_call_id

        if message.tool_calls:
            serialized["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.raw_arguments,
                    },
                }
                for tc in message.tool_calls
            ]

        return serialized

    def _serialize_content(
        self,
        content: str | list[TextContent | ImageContent] | None,
    ) -> str | list[dict]:
        if content is None:
            return None

        if isinstance(content, str):
            return content

        parts: list[dict] = []
        for part in content:
            if isinstance(part, TextContent):
                parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ImageContent):
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": part.url,
                            "detail": part.detail,
                        },
                    }
                )
        return parts

    # ------------------------------------------------------------------
    # Parse response
    # ------------------------------------------------------------------

    def parse_response(
        self,
        raw_response: Any,
        profile: ModelProfile,
    ) -> LLMResponse:
        choice = raw_response.choices[0] if raw_response.choices else None

        if choice is None:
            return LLMResponse(content=None)

        content = choice.message.content
        reasoning = getattr(choice.message, "reasoning_content", None)

        # SiliconFlow 等有时把视觉内容放 reasoning_content
        # 如果 content 为空但有 reasoning，提升为主要内容
        if not content and reasoning:
            content = reasoning
            reasoning = None

        tool_calls_raw = choice.message.tool_calls or []

        tool_calls: list[ToolCall] = []
        for tc in tool_calls_raw:
            try:
                arguments = json.loads(tc.function.arguments)
                parse_error = None
            except json.JSONDecodeError as e:
                arguments = None
                parse_error = str(e)

            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    raw_arguments=tc.function.arguments,
                    arguments=arguments,
                    parse_error=parse_error,
                )
            )

        thinking = reasoning

        usage = None
        if raw_response.usage is not None:
            usage = TokenUsage(
                input_tokens=raw_response.usage.prompt_tokens,
                output_tokens=raw_response.usage.completion_tokens,
                total_tokens=(
                    raw_response.usage.total_tokens
                    or (
                        raw_response.usage.prompt_tokens
                        + raw_response.usage.completion_tokens
                    )
                    if raw_response.usage.prompt_tokens is not None
                    and raw_response.usage.completion_tokens is not None
                    else None
                ),
                cache_read_tokens=getattr(
                    raw_response.usage, "prompt_cache_hit_tokens", None
                ),
                cache_write_tokens=getattr(
                    raw_response.usage, "prompt_cache_miss_tokens", None
                ),
            )

        finish_reason = _OPENAI_FINISH_REASON_MAP.get(
            choice.finish_reason, choice.finish_reason
        )

        return LLMResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            thinking=thinking,
            finish_reason=finish_reason,
            model=raw_response.model,
            provider=self.name,
            provider_response_id=raw_response.id,
            usage=usage,
        )

    # ------------------------------------------------------------------
    # Parse stream chunk
    # ------------------------------------------------------------------

    def parse_stream_chunk(
        self,
        chunk: Any,
    ) -> tuple[LLMStreamEvent, ...]:
        events: list[LLMStreamEvent] = []

        if len(chunk.choices) == 0:
            if chunk.usage is not None:
                events.append(
                    UsageDelta(
                        usage=TokenUsage(
                            input_tokens=chunk.usage.prompt_tokens,
                            output_tokens=chunk.usage.completion_tokens,
                            total_tokens=(
                                chunk.usage.total_tokens
                                or (
                                    chunk.usage.prompt_tokens
                                    + chunk.usage.completion_tokens
                                )
                                if chunk.usage.prompt_tokens is not None
                                and chunk.usage.completion_tokens is not None
                                else None
                            ),
                            cache_read_tokens=getattr(
                                chunk.usage, "prompt_cache_hit_tokens", None
                            ),
                            cache_write_tokens=getattr(
                                chunk.usage, "prompt_cache_miss_tokens", None
                            ),
                        )
                    )
                )
            return tuple(events)

        choice = chunk.choices[0]
        delta = choice.delta

        if delta.content:
            events.append(ContentDelta(text=delta.content))

        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            events.append(ThinkingDelta(text=reasoning))

        if delta.tool_calls:
            for tc in delta.tool_calls:
                events.append(
                    ToolCallDelta(
                        index=tc.index,
                        call_id_delta=tc.id,
                        name_delta=tc.function.name,
                        arguments_delta=tc.function.arguments,
                    )
                )

        if choice.finish_reason is not None:
            finish_reason = _OPENAI_FINISH_REASON_MAP.get(
                choice.finish_reason, choice.finish_reason
            )
            events.append(StreamCompleted(finish_reason=finish_reason))

        return tuple(events)

    # ------------------------------------------------------------------
    # Map error
    # ------------------------------------------------------------------

    def map_error(
        self,
        exc: Exception,
    ) -> LLMError:
        if isinstance(exc, LLMError):
            return exc

        if isinstance(exc, APITimeoutError):
            return LLMTimeoutError(
                code="request_timeout",
                message=str(exc) or "LLM request timed out",
                retryable=True,
                provider=self.name,
            )

        if isinstance(exc, APIConnectionError):
            return LLMConnectionError(
                code="connection_error",
                message=str(exc) or "LLM connection failed",
                retryable=True,
                provider=self.name,
            )

        if isinstance(exc, AuthenticationError):
            return LLMAuthenticationError(
                code="authentication_error",
                message="LLM authentication failed",
                retryable=False,
                provider=self.name,
                status_code=exc.status_code,
            )

        if isinstance(exc, RateLimitError):
            retry_after = None
            if exc.response is not None:
                retry_after = _parse_retry_after(exc.response)
            return LLMRateLimitError(
                code="rate_limit",
                message=str(exc) or "Rate limit exceeded",
                retryable=True,
                retry_after=retry_after,
                provider=self.name,
                status_code=exc.status_code,
            )

        if isinstance(exc, BadRequestError):
            body = _error_body(exc)
            error_data = (body or {}).get("error", {}) or {}
            code = error_data.get("code", "") if isinstance(error_data, dict) else ""
            if "content_filter" in str(exc).lower():
                return ContentSafetyError(
                    code="content_safety",
                    message=str(exc),
                    retryable=False,
                    provider=self.name,
                    status_code=exc.status_code,
                )
            if code == "context_length_exceeded":
                return ContextLengthError(
                    code="context_length",
                    message=str(exc),
                    retryable=False,
                    provider=self.name,
                    status_code=exc.status_code,
                )
            return LLMError(
                code="bad_request",
                message=str(exc),
                retryable=False,
                provider=self.name,
                status_code=exc.status_code,
            )

        if isinstance(exc, InternalServerError):
            return LLMError(
                code="server_error",
                message=str(exc),
                retryable=True,
                provider=self.name,
                status_code=exc.status_code,
            )

        if isinstance(exc, APIResponseValidationError):
            return InvalidLLMResponseError(
                code="invalid_response",
                message=str(exc),
                retryable=False,
                provider=self.name,
            )

        if isinstance(exc, APIError):
            return LLMError(
                code="api_error",
                message=str(exc),
                retryable=exc.status_code is None or exc.status_code >= 500,
                provider=self.name,
                status_code=exc.status_code,
            )

        return LLMError(
            code="unknown_error",
            message=str(exc),
            retryable=False,
            provider=self.name,
        )


def _parse_retry_after(response: Any) -> float | None:
    try:
        value = response.headers.get("retry-after", response.headers.get("Retry-After"))
        if value is not None:
            return float(value)
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _error_body(exc: APIError) -> dict | None:
    try:
        if exc.body and isinstance(exc.body, dict):
            return exc.body
        if exc.response is not None:
            import json
            return json.loads(exc.response.text)
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        pass
    return None
