"""Tests for OpenAI-compatible Provider with Fake HTTP transport.

覆盖场景：
- 成功响应解析
- 401、400、429、500 HTTP 错误映射
- 超时和连接错误
- 空回复和 Tool Call 明确失败
- 单元测试不访问网络
"""

from __future__ import annotations

import json

import httpx
import pytest

from cogito.model.contracts import (
    ErrorCategory,
    FinishReason,
    ModelRequest,
)
from cogito.model.errors import ModelProviderError
from cogito.model.openai_compat import OpenAICompatProvider


def _make_provider(responses: list[httpx.Response] | None = None) -> OpenAICompatProvider:
    """创建使用 Fake Transport 的 Provider。"""
    provider = OpenAICompatProvider(
        model="test-model",
        api_key="sk-test",
        base_url="http://fake.local/v1",
        timeout_seconds=5,
    )

    if responses:
        transport = httpx.MockTransport(
            lambda request: responses.pop(0)
        )
        provider._client = httpx.AsyncClient(
            transport=transport,
            base_url=provider._base_url,
            timeout=httpx.Timeout(5),
        )

    return provider


def _success_response(text: str = "Hello!") -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json={
            "id": "chatcmpl-xxx",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
            },
        },
    )


def _make_request(override: dict | None = None) -> ModelRequest:
    msg = {"role": "user", "content": "Hello"}
    return ModelRequest(
        messages=(msg,),
        max_output_tokens=100,
        temperature=0.7,
        **(override or {}),
    )


# =============================================================================
# 成功场景
# =============================================================================


class TestSuccess:
    @pytest.mark.asyncio
    async def test_simple_response(self):
        provider = _make_provider([_success_response("Hi there!")])
        response = await provider.generate(_make_request())

        assert response.text == "Hi there!"
        assert response.finish_reason == FinishReason.stop
        assert response.usage.input_tokens == 10
        assert response.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_empty_content(self):
        """空内容但 stop finish_reason 也是合法响应。"""
        provider = _make_provider([_success_response("")])
        response = await provider.generate(_make_request())

        assert response.text == ""
        assert response.finish_reason == FinishReason.stop

    @pytest.mark.asyncio
    async def test_provider_request_id(self):
        provider = _make_provider([_success_response("Hello")])
        response = await provider.generate(_make_request())
        assert response.provider_request_id == "chatcmpl-xxx"

    @pytest.mark.asyncio
    async def test_max_tokens_and_temp_in_request(self):
        """max_tokens 和 temperature 正确发送。"""
        sent_payload = {}

        def capture(request: httpx.Request) -> httpx.Response:
            nonlocal sent_payload
            sent_payload = json.loads(request.content)
            return _success_response("OK")

        transport = httpx.MockTransport(capture)
        provider = _make_provider()
        provider._client = httpx.AsyncClient(
            transport=transport,
            base_url=provider._base_url,
            timeout=httpx.Timeout(5),
        )

        await provider.generate(_make_request())
        assert sent_payload.get("max_tokens") == 100
        assert sent_payload.get("temperature") == 0.7
        assert sent_payload.get("model") == "test-model"
        assert sent_payload.get("stream") is False


# =============================================================================
# HTTP 错误映射
# =============================================================================


class TestHTTPErrors:
    @pytest.mark.asyncio
    async def test_401_maps_to_authentication(self):
        provider = _make_provider([
            httpx.Response(status_code=401, json={"error": {"message": "Invalid API key"}}),
        ])
        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(_make_request())
        assert exc.value.envelope.category == ErrorCategory.authentication
        assert exc.value.envelope.retryable is False

    @pytest.mark.asyncio
    async def test_400_maps_to_invalid_request(self):
        provider = _make_provider([
            httpx.Response(status_code=400, json={"error": {"message": "Bad request"}}),
        ])
        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(_make_request())
        assert exc.value.envelope.category == ErrorCategory.invalid_request
        assert exc.value.envelope.retryable is False

    @pytest.mark.asyncio
    async def test_429_maps_to_rate_limit(self):
        provider = _make_provider([
            httpx.Response(status_code=429, json={"error": {"message": "Rate limited"}}),
        ])
        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(_make_request())
        assert exc.value.envelope.category == ErrorCategory.rate_limit
        assert exc.value.envelope.retryable is True

    @pytest.mark.asyncio
    async def test_500_maps_to_provider_internal_retryable(self):
        provider = _make_provider([
            httpx.Response(status_code=500, text="Internal error"),
        ])
        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(_make_request())
        assert exc.value.envelope.category == ErrorCategory.provider_internal
        assert exc.value.envelope.retryable is True

    @pytest.mark.asyncio
    async def test_404_maps_to_model_not_found(self):
        provider = _make_provider([
            httpx.Response(status_code=404, json={"error": {"message": "Not found"}}),
        ])
        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(_make_request())
        assert exc.value.envelope.category == ErrorCategory.model_not_found
        assert exc.value.envelope.retryable is False


# =============================================================================
# 超时和连接错误
# =============================================================================


class TestNetworkErrors:
    @pytest.mark.asyncio
    async def test_timeout(self):
        """超时映射为 timeout 错误。"""
        provider = _make_provider()

        # Mock the client's post to raise TimeoutException
        import httpx
        async def raise_timeout(*args, **kwargs):
            raise httpx.TimeoutException("Request timed out", request=None)

        provider._client.post = raise_timeout

        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(_make_request())
        assert exc.value.envelope.category == ErrorCategory.timeout

    @pytest.mark.asyncio
    async def test_connection_error(self):
        provider = _make_provider()

        async def raise_connection_error(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused", request=request)

        transport = httpx.MockTransport(raise_connection_error)
        provider._client = httpx.AsyncClient(
            transport=transport,
            base_url=provider._base_url,
            timeout=httpx.Timeout(5),
        )

        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(_make_request())
        assert exc.value.envelope.category == ErrorCategory.connection


# =============================================================================
# 不支持功能
# =============================================================================


class TestUnsupported:
    @pytest.mark.asyncio
    async def test_streaming_raises_error(self):
        provider = _make_provider([_success_response("Stream")])
        request = _make_request({"stream": True})

        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(request)
        assert exc.value.envelope.category == ErrorCategory.invalid_request


# =============================================================================
# 工具调用（Phase 2）
# =============================================================================


class TestToolCalls:
    def _tool_response(self, content: str = "") -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "id": "chatcmpl-tool",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": content,
                            "tool_calls": [
                                {
                                    "id": "call_abc123",
                                    "type": "function",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text": "hello"}',
                                    },
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    def _capture_provider(self, sent_storage: dict) -> OpenAICompatProvider:
        """创建将请求体捕获到 sent_storage 的 provider。"""
        def capture(request: httpx.Request) -> httpx.Response:
            sent_storage["payload"] = json.loads(request.content)
            return self._tool_response("done")

        transport = httpx.MockTransport(capture)
        provider = _make_provider()
        provider._client = httpx.AsyncClient(
            transport=transport,
            base_url=provider._base_url,
            timeout=httpx.Timeout(5),
        )
        return provider

    @pytest.mark.asyncio
    async def test_tool_calls_in_request_payload(self):
        """tools 参数正确发送到请求体。"""
        sent = {}
        provider = self._capture_provider(sent)

        request = ModelRequest(
            messages=({"role": "user", "content": "Hello"},),
            tools=({"type": "function", "function": {
                "name": "echo", "description": "Echo text",
                "parameters": {"type": "object", "properties": {}},
            }},),
        )
        await provider.generate(request)

        assert "tools" in sent["payload"]
        assert sent["payload"]["tools"][0]["function"]["name"] == "echo"

    @pytest.mark.asyncio
    async def test_assistant_tool_calls_in_message(self):
        """assistant 消息的 tool_calls 正确序列化到请求体。"""
        sent = {}
        provider = self._capture_provider(sent)

        request = ModelRequest(
            messages=(
                {"role": "user", "content": "Use echo tool"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call1", "type": "function",
                                "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]},
                {"role": "tool", "content": "hi", "tool_call_id": "call1"},
            ),
        )
        await provider.generate(request)

        msgs = sent["payload"]["messages"]
        assert msgs[1]["role"] == "assistant"
        assert "tool_calls" in msgs[1]
        assert msgs[1]["tool_calls"][0]["function"]["name"] == "echo"

        assert msgs[2]["role"] == "tool"
        assert msgs[2]["tool_call_id"] == "call1"
        assert msgs[2]["content"] == "hi"

    @pytest.mark.asyncio
    async def test_tool_message_without_content(self):
        """tool 消息无 content 时正确序列化。"""
        sent = {}
        provider = self._capture_provider(sent)

        request = ModelRequest(
            messages=({"role": "tool", "tool_call_id": "call1"},),
        )
        await provider.generate(request)

        msgs = sent["payload"]["messages"]
        assert msgs[0]["role"] == "tool"
        assert msgs[0]["tool_call_id"] == "call1"
        assert "content" in msgs[0]

    @pytest.mark.asyncio
    async def test_assistant_tool_calls_with_text(self):
        """assistant 同时返回文本和 tool_calls 时两者都序列化。"""
        sent = {}
        provider = self._capture_provider(sent)

        request = ModelRequest(
            messages=(
                {"role": "assistant", "content": "I will use a tool",
                 "tool_calls": [{"id": "call1", "type": "function",
                                "function": {"name": "echo", "arguments": '{}'}}]},
            ),
        )
        await provider.generate(request)

        msgs = sent["payload"]["messages"]
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["content"] == "I will use a tool"
        assert "tool_calls" in msgs[0]

    @pytest.mark.asyncio
    async def test_parse_tool_calls_response(self):
        provider = _make_provider([self._tool_response("I'll use echo")])
        request = _make_request({
            "tools": ({
                "type": "function",
                "function": {"name": "echo", "description": "", "parameters": {}},
            },),
        })

        response = await provider.generate(request)
        assert response.finish_reason == FinishReason.tool_calls
        assert len(response.tool_calls) == 1

        tc = response.tool_calls[0]
        assert tc["id"] == "call_abc123"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "echo"
        assert tc["function"]["arguments"] == '{"text": "hello"}'

    @pytest.mark.asyncio
    async def test_parse_tool_calls_with_content(self):
        """模型同时返回文本和 tool_calls。"""
        provider = _make_provider([self._tool_response("I will help you.")])
        request = _make_request({"tools": ({},)})

        response = await provider.generate(request)
        assert response.finish_reason == FinishReason.tool_calls
        assert "I will help you" in response.text
        assert len(response.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_tool_calls_not_raises_error(self):
        """请求中带 tools 不再报错。"""
        provider = _make_provider([_success_response("OK")])
        request = _make_request({"tools": ({
            "type": "function",
            "function": {"name": "test", "parameters": {}},
        },)})

        # 不应抛出异常
        response = await provider.generate(request)
        assert response.finish_reason == FinishReason.stop
        assert response.text == "OK"

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self):
        """多个并行 tool_calls。"""
        response_data = {
            "id": "chatcmpl-parallel",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "echo", "arguments": '{"text": "a"}'},
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "echo", "arguments": '{"text": "b"}'},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20},
        }

        provider = _make_provider([httpx.Response(status_code=200, json=response_data)])
        request = _make_request({"tools": ({}, {})})

        response = await provider.generate(request)
        assert response.finish_reason == FinishReason.tool_calls
        assert len(response.tool_calls) == 2
        assert response.tool_calls[0]["id"] == "call_1"
        assert response.tool_calls[1]["id"] == "call_2"


# =============================================================================
# 响应解析
# =============================================================================


class TestResponseParsing:
    @pytest.mark.asyncio
    async def test_invalid_json(self):
        """非法 JSON 响应。"""
        provider = _make_provider([
            httpx.Response(status_code=200, text="not json"),
        ])
        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(_make_request())
        assert exc.value.envelope.category == ErrorCategory.provider_internal
