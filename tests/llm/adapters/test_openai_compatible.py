"""Tests for OpenAICompatibleAdapter."""

import json
from unittest.mock import MagicMock, PropertyMock

import pytest

from cogito.llm.adapters.openai_compatible import OpenAICompatibleAdapter
from cogito.llm.capabilities import ModelCapabilities, ModelProfile
from cogito.llm.errors import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMTimeoutError,
    ContentSafetyError,
    ContextLengthError,
    InvalidLLMResponseError,
)
from cogito.llm.request import (
    ChatMessage,
    ChatRequest,
    ImageContent,
    TextContent,
    ToolDefinition,
)
from cogito.llm.response import ToolCall


@pytest.fixture
def adapter():
    return OpenAICompatibleAdapter()


@pytest.fixture
def profile():
    caps = ModelCapabilities(tools=True, vision=True, thinking=True)
    return ModelProfile(
        name="test",
        provider="test-provider",
        model="test-model",
        capabilities=caps,
        max_output_tokens=4096,
    )


class TestBuildRequest:
    def test_minimal(self, adapter, profile):
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile, request, stream=False)

        assert payload["model"] == "test-model"
        assert len(payload["messages"]) == 1
        assert payload["stream"] is False
        assert "stream_options" not in payload

    def test_stream_mode(self, adapter, profile):
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile, request, stream=True)

        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}

    def test_max_tokens_from_request(self, adapter, profile):
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),), max_output_tokens=2048)
        payload = adapter.build_request(profile, request, stream=False)
        assert payload["max_tokens"] == 2048

    def test_max_tokens_from_profile(self, adapter, profile):
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile, request, stream=False)
        assert payload["max_tokens"] == 4096

    def test_temperature_top_p_stop(self, adapter, profile):
        request = ChatRequest(
            messages=(ChatMessage(role="user", content="Hi"),),
            temperature=0.7,
            top_p=0.9,
            stop=("\n\n", "END"),
        )
        payload = adapter.build_request(profile, request, stream=False)
        assert payload["temperature"] == 0.7
        assert payload["top_p"] == 0.9
        assert payload["stop"] == ["\n\n", "END"]

    def test_with_tools(self, adapter, profile):
        td = ToolDefinition(
            name="get_weather",
            description="Get weather",
            parameters={"type": "object", "properties": {"loc": {"type": "string"}}},
        )
        request = ChatRequest(
            messages=(ChatMessage(role="user", content="Weather?"),),
            tools=(td,),
            tool_choice="auto",
        )
        payload = adapter.build_request(profile, request, stream=False)

        assert len(payload["tools"]) == 1
        assert payload["tools"][0]["function"]["name"] == "get_weather"
        assert payload["tool_choice"] == "auto"

    def test_with_extra_body(self, adapter):
        caps = ModelCapabilities(tools=True, vision=True, thinking=True)
        profile = ModelProfile(
            name="test",
            provider="test-provider",
            model="test-model",
            capabilities=caps,
            default_extra_body={"thinking": {"type": "enabled"}},
        )
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile, request, stream=False)

        assert payload["extra_body"] == {"thinking": {"type": "enabled"}}


class TestSerializeMessage:
    def test_text_content(self, adapter):
        msg = ChatMessage(role="user", content="Hello")
        result = adapter._serialize_message(msg)
        assert result == {"role": "user", "content": "Hello"}

    def test_content_parts(self, adapter):
        parts = [TextContent(text="desc"), ImageContent(url="https://example.com/img.png", detail="high")]
        msg = ChatMessage(role="user", content=parts)
        result = adapter._serialize_message(msg)

        assert result["role"] == "user"
        assert len(result["content"]) == 2
        assert result["content"][0] == {"type": "text", "text": "desc"}
        assert result["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/img.png", "detail": "high"},
        }

    def test_null_content(self, adapter):
        msg = ChatMessage(role="assistant", content=None)
        result = adapter._serialize_message(msg)
        assert result["content"] is None

    def test_tool_call_id(self, adapter):
        msg = ChatMessage(role="tool", content="result", tool_call_id="call_1")
        result = adapter._serialize_message(msg)
        assert result["tool_call_id"] == "call_1"


class TestParseResponse:
    def test_basic_text(self, adapter, profile):
        raw = _make_response(content="Hello!")
        resp = adapter.parse_response(raw, profile)

        assert resp.content == "Hello!"
        assert resp.tool_calls == ()
        assert resp.model == "test-model"
        assert resp.provider == "openai_compatible"

    def test_with_tool_calls(self, adapter, profile):
        raw = _make_response(
            content=None,
            tool_calls=[
                {"id": "call_1", "function": {"name": "get_weather", "arguments": '{"loc": "NYC"}'}}
            ],
            finish_reason="tool_calls",
        )
        resp = adapter.parse_response(raw, profile)

        assert resp.content is None
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "get_weather"
        assert resp.tool_calls[0].raw_arguments == '{"loc": "NYC"}'
        assert resp.tool_calls[0].arguments == {"loc": "NYC"}
        assert resp.finish_reason == "tool_calls"

    def test_tool_call_with_invalid_json(self, adapter, profile):
        raw = _make_response(
            content=None,
            tool_calls=[
                {"id": "call_1", "function": {"name": "f", "arguments": "not-json"}}
            ],
            finish_reason="tool_calls",
        )
        resp = adapter.parse_response(raw, profile)

        assert resp.tool_calls[0].arguments is None
        assert resp.tool_calls[0].parse_error is not None

    def test_with_usage(self, adapter, profile):
        raw = _make_response(content="Hi", usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
        resp = adapter.parse_response(raw, profile)

        assert resp.usage is not None
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 20
        assert resp.usage.total_tokens == 30

    def test_no_choices(self, adapter, profile):
        raw = MagicMock()
        raw.choices = []
        raw.usage = None
        raw.model = "test-model"
        raw.id = "resp_1"

        resp = adapter.parse_response(raw, profile)
        assert resp.content is None

    def test_finish_reason_mapping(self, adapter, profile):
        raw = _make_response(content="Hello", finish_reason="stop")
        resp = adapter.parse_response(raw, profile)
        assert resp.finish_reason == "stop"


class TestParseStreamChunk:
    def test_content_delta(self, adapter):
        chunk = _make_chunk(delta_content="Hello")
        events = adapter.parse_stream_chunk(chunk)

        assert len(events) == 1
        assert events[0].text == "Hello"

    def test_multiple_deltas_in_one_chunk(self, adapter):
        chunk = _make_chunk(delta_content=" world", finish_reason="stop")
        events = adapter.parse_stream_chunk(chunk)

        assert len(events) == 2
        assert events[0].text == " world"
        assert events[1].finish_reason == "stop"

    def empty_choices_with_usage(self, adapter):
        chunk = MagicMock()
        chunk.choices = []

        usage = MagicMock()
        usage.prompt_tokens = 10
        usage.completion_tokens = 20
        usage.total_tokens = 30
        chunk.usage = usage

        type(chunk).usage = PropertyMock(return_value=usage)

        events = adapter.parse_stream_chunk(chunk)

        assert len(events) == 1

    def test_tool_call_delta(self, adapter):
        chunk = _make_chunk(
            delta_tool_calls=[
                {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": '{"lo'}}
            ]
        )
        events = adapter.parse_stream_chunk(chunk)

        assert len(events) == 1
        assert events[0].index == 0
        assert events[0].call_id_delta == "call_1"
        assert events[0].name_delta == "get_weather"
        assert events[0].arguments_delta == '{"lo'

    def test_thinking_delta(self, adapter):
        chunk = _make_chunk(delta_content="answer", delta_reasoning="I think")
        events = adapter.parse_stream_chunk(chunk)

        thinking_events = [e for e in events if hasattr(e, "text") and "I think" in e.text]

    def test_no_events(self, adapter):
        chunk = _make_chunk()
        events = adapter.parse_stream_chunk(chunk)
        assert len(events) == 0


class TestMapError:
    def test_timeout_error(self, adapter):
        from openai import APITimeoutError
        exc = APITimeoutError("timed out")
        mapped = adapter.map_error(exc)

        assert isinstance(mapped, LLMTimeoutError)
        assert mapped.retryable is True

    def test_connection_error(self, adapter):
        from openai import APIConnectionError
        import httpx
        exc = APIConnectionError(message="connection failed", request=MagicMock(spec=httpx.Request))
        mapped = adapter.map_error(exc)

        assert isinstance(mapped, LLMConnectionError)
        assert mapped.retryable is True

    def test_authentication_error(self, adapter):
        from openai import AuthenticationError
        exc = AuthenticationError(
            "bad key",
            response=MagicMock(status_code=401),
            body={"error": "unauthorized"},
        )
        mapped = adapter.map_error(exc)

        assert isinstance(mapped, LLMAuthenticationError)
        assert mapped.retryable is False

    def test_rate_limit_error(self, adapter):
        from openai import RateLimitError
        response = MagicMock(status_code=429)
        response.headers = {"retry-after": "30"}
        exc = RateLimitError("rate limit", response=response, body={"error": "rate limit"})
        mapped = adapter.map_error(exc)

        assert isinstance(mapped, LLMRateLimitError)
        assert mapped.retryable is True
        assert mapped.retry_after == 30.0

    def test_content_filter_error(self, adapter):
        from openai import BadRequestError
        exc = BadRequestError(
            "content_filter triggered",
            response=MagicMock(status_code=400),
            body={"error": {"code": "content_filter"}},
        )
        mapped = adapter.map_error(exc)

        assert isinstance(mapped, ContentSafetyError)
        assert mapped.retryable is False

    def test_context_length_error(self, adapter):
        from openai import BadRequestError
        exc = BadRequestError(
            "maximum context length exceeded",
            response=MagicMock(status_code=400),
            body={"error": {"code": "context_length_exceeded"}},
        )
        mapped = adapter.map_error(exc)

        assert isinstance(mapped, ContextLengthError)
        assert mapped.retryable is False

    def test_server_error(self, adapter):
        from openai import InternalServerError
        exc = InternalServerError("server error", response=MagicMock(status_code=500), body={"error": "internal"})
        mapped = adapter.map_error(exc)

        assert mapped.retryable is True
        assert mapped.status_code == 500

    def test_invalid_response(self, adapter):
        from openai import APIResponseValidationError
        exc = APIResponseValidationError(
            response=MagicMock(status_code=400),
            body={"error": "invalid"},
            message="invalid response",
        )
        mapped = adapter.map_error(exc)

        assert isinstance(mapped, InvalidLLMResponseError)

    def test_unknown_error(self, adapter):
        exc = ValueError("something weird")
        mapped = adapter.map_error(exc)

        assert mapped.code == "unknown_error"
        assert mapped.retryable is False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_response(content=None, tool_calls=None, finish_reason="stop", usage=None):
    """Create a MagicMock that mimics an OpenAI ChatCompletion response."""
    mock = MagicMock()
    mock.model = "test-model"
    mock.id = "resp_1"

    choice = MagicMock()
    choice.finish_reason = finish_reason

    message = MagicMock()
    message.content = content
    message.tool_calls = None
    message.reasoning_content = None

    if tool_calls:
        tc_mocks = []
        for tc in tool_calls:
            tc_mock = MagicMock()
            tc_mock.id = tc["id"]
            tc_mock.type = "function"
            tc_mock.function.name = tc["function"]["name"]
            tc_mock.function.arguments = tc["function"]["arguments"]
            tc_mocks.append(tc_mock)
        message.tool_calls = tc_mocks

    choice.message = message

    if usage:
        u = MagicMock()
        u.prompt_tokens = usage.get("prompt_tokens")
        u.completion_tokens = usage.get("completion_tokens")
        u.total_tokens = usage.get("total_tokens")
        mock.usage = u
    else:
        mock.usage = None

    mock.choices = [choice]
    return mock


def _make_chunk(delta_content=None, finish_reason=None, delta_tool_calls=None, delta_reasoning=None):
    """Create a MagicMock that mimics an OpenAI streaming chunk."""
    chunk = MagicMock()
    chunk.choices = []
    chunk.usage = None

    choice = MagicMock()
    choice.finish_reason = finish_reason

    delta = MagicMock()
    delta.content = delta_content
    delta.tool_calls = None
    delta.reasoning_content = delta_reasoning

    if delta_tool_calls:
        tc_mocks = []
        for tc in delta_tool_calls:
            tc_mock = MagicMock()
            tc_mock.index = tc["index"]
            tc_mock.id = tc.get("id")
            tc_mock.function.name = tc["function"]["name"]
            tc_mock.function.arguments = tc["function"]["arguments"]
            tc_mocks.append(tc_mock)
        delta.tool_calls = tc_mocks

    choice.delta = delta
    chunk.choices = [choice]
    return chunk
