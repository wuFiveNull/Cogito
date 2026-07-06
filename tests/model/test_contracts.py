"""Tests for Model Contracts (PR 9-A) and Stub Provider (PR 9-B).

覆盖场景：
- ModelRequest/Response round-trip
- 不支持 ContentPart 明确失败
- Finish Reason 规范化
- Usage 映射
- Provider 错误映射与脱敏
- Capability 校验
"""

from __future__ import annotations

import pytest

from cogito.model.contracts import (
    ContentPart,
    ContentPartType,
    ErrorCategory,
    ErrorEnvelope,
    FinishReason,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Usage,
    classify_error,
    normalize_finish_reason,
)
from cogito.model.stub_provider import StubModelProvider, StubScenario

# =============================================================================
# 模型契约测试
# =============================================================================


class TestModelRequest:
    def test_request_has_id(self):
        req = ModelRequest()
        assert len(req.request_id) > 0

    def test_request_is_frozen(self):
        req = ModelRequest()
        with pytest.raises(AttributeError):
            req.request_id = "new_id"  # type: ignore[misc]

    def test_request_repr_no_secret(self):
        """repr 不显示 Secret。"""
        req = ModelRequest()
        r = repr(req)
        assert "secret" not in r.lower()
        assert "key" not in r.lower()

    def test_request_with_messages(self):
        req = ModelRequest(
            messages=[{"role": "user", "content": "Hello"}],
        )
        assert len(req.messages) == 1

    def test_request_with_tools(self):
        req = ModelRequest(tools=[{"name": "test_tool"}])
        assert len(req.tools) == 1
        # tools 是 tuple
        assert isinstance(req.tools, tuple)


class TestModelResponse:
    def test_response_has_finish_reason(self):
        resp = ModelResponse(finish_reason=FinishReason.stop)
        assert resp.finish_reason == FinishReason.stop

    def test_response_text_property(self):
        resp = ModelResponse(
            content_parts=(
                ContentPart(part_type=ContentPartType.text, text="Hello "),
                ContentPart(part_type=ContentPartType.text, text="World"),
            ),
        )
        assert resp.text == "Hello World"

    def test_response_with_usage(self):
        usage = Usage(input_tokens=100, output_tokens=50)
        resp = ModelResponse(usage=usage)
        assert resp.usage.input_tokens == 100
        assert resp.usage.output_tokens == 50

    def test_response_is_frozen(self):
        resp = ModelResponse()
        with pytest.raises(AttributeError):
            resp.request_id = "new"

    def test_response_repr_no_secret(self):
        resp = ModelResponse()
        r = repr(resp)
        assert "secret" not in r.lower()
        assert "key" not in r.lower()


class TestUsage:
    def test_usage_addition(self):
        u1 = Usage(input_tokens=10, output_tokens=20)
        u2 = Usage(input_tokens=5, output_tokens=10)
        total = u1 + u2
        assert total.input_tokens == 15
        assert total.output_tokens == 30

    def test_usage_immutable(self):
        u = Usage(input_tokens=10, output_tokens=20)
        with pytest.raises(AttributeError):
            u.input_tokens = 5  # type: ignore[misc]


class TestFinishReasonNormalization:
    def test_stop(self):
        assert normalize_finish_reason("stop") == FinishReason.stop
        assert normalize_finish_reason("end_turn") == FinishReason.stop
        assert normalize_finish_reason("completed") == FinishReason.stop

    def test_tool_calls(self):
        assert normalize_finish_reason("tool_calls") == FinishReason.tool_calls
        assert normalize_finish_reason("tool_use") == FinishReason.tool_calls

    def test_length(self):
        assert normalize_finish_reason("length") == FinishReason.length
        assert normalize_finish_reason("max_tokens") == FinishReason.length

    def test_content_filter(self):
        assert normalize_finish_reason("content_filter") == FinishReason.content_filter

    def test_cancelled(self):
        assert normalize_finish_reason("cancelled") == FinishReason.cancelled
        assert normalize_finish_reason("cancel") == FinishReason.cancelled

    def test_error(self):
        assert normalize_finish_reason("error") == FinishReason.error
        assert normalize_finish_reason("unknown_reason") == FinishReason.error


class TestErrorClassification:
    def test_authentication(self):
        err = classify_error("authentication_error", 401)
        assert err.category == ErrorCategory.authentication
        assert err.retryable is False

    def test_rate_limit(self):
        err = classify_error("rate_limit_exceeded", 429)
        assert err.category == ErrorCategory.rate_limit
        assert err.retryable is True
        assert err.retry_after is not None

    def test_context_overflow(self):
        err = classify_error("context_length_exceeded")
        assert err.category == ErrorCategory.context_overflow
        assert err.retryable is False

    def test_timeout(self):
        err = classify_error("timeout")
        assert err.category == ErrorCategory.timeout
        assert err.retryable is True

    def test_connection(self):
        err = classify_error("connection_error")
        assert err.category == ErrorCategory.connection
        assert err.retryable is True

    def test_content_filter(self):
        err = classify_error("content_filter")
        assert err.category == ErrorCategory.content_filter
        assert err.retryable is False

    def test_model_not_found(self):
        err = classify_error("model_not_found", 404)
        assert err.category == ErrorCategory.model_not_found
        assert err.retryable is False

    def test_provider_internal(self):
        err = classify_error("internal_server_error", 500)
        assert err.category == ErrorCategory.provider_internal
        assert err.retryable is True

    def test_unknown_error_defaults(self):
        err = classify_error("some_weird_error")
        assert err.category == ErrorCategory.provider_internal
        assert err.retryable is True


class TestModelCapabilities:
    def test_default_capabilities(self):
        caps = ModelCapabilities()
        assert caps.context_window == 0
        assert caps.modalities == ("text",)

    def test_capabilities_immutable(self):
        caps = ModelCapabilities(context_window=128000)
        with pytest.raises(AttributeError):
            caps.context_window = 64000  # type: ignore[misc]


class TestContentPart:
    def test_text_content(self):
        part = ContentPart(part_type=ContentPartType.text, text="Hello")
        assert part.text == "Hello"
        assert part.part_type == ContentPartType.text

    def test_content_part_immutable(self):
        part = ContentPart(text="Hello")
        with pytest.raises(AttributeError):
            part.text = "World"  # type: ignore[misc]

    def test_content_part_repr(self):
        part = ContentPart(text="Hello World This Is A Long Text")
        r = repr(part)
        assert "ContentPart" in r
        assert "Hello" in r


# =============================================================================
# Stub Provider 测试
# =============================================================================


class TestStubProvider:
    @pytest.mark.asyncio
    async def test_generate_returns_response(self):
        provider = StubModelProvider()
        request = ModelRequest(messages=[{"role": "user", "content": "Hi"}])

        response = await provider.generate(request)
        assert response.finish_reason == FinishReason.stop
        assert "stub response" in response.text
        assert response.usage.input_tokens == 10
        assert response.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_generate_records_request(self):
        provider = StubModelProvider()
        request = ModelRequest(messages=[{"role": "user", "content": "Hi"}])

        await provider.generate(request)
        assert len(provider.received_requests) == 1
        assert provider.received_requests[0] is request

    @pytest.mark.asyncio
    async def test_multiple_calls_cycle_scenarios(self):
        provider = StubModelProvider([
            StubScenario(response_text="First"),
            StubScenario(response_text="Second"),
        ])

        r1 = await provider.generate(ModelRequest())
        r2 = await provider.generate(ModelRequest())
        r3 = await provider.generate(ModelRequest())  # cycles back

        assert r1.text == "First"
        assert r2.text == "Second"
        assert r3.text == "First"  # cycle

    @pytest.mark.asyncio
    async def test_error_scenario(self):
        from cogito.model.errors import ModelProviderError

        provider = StubModelProvider([
            StubScenario(error=ErrorEnvelope(
                category=ErrorCategory.rate_limit,
                message="Rate limited",
                retryable=True,
            )),
        ])

        with pytest.raises(ModelProviderError) as exc:
            await provider.generate(ModelRequest())
        assert exc.value.envelope.category == ErrorCategory.rate_limit

    @pytest.mark.asyncio
    async def test_capabilities(self):
        provider = StubModelProvider()
        caps = provider.capabilities()
        assert caps.context_window == 128000
        assert caps.max_output_tokens == 4096
        assert caps.supports_tools is False

    @pytest.mark.asyncio
    async def test_health(self):
        provider = StubModelProvider()
        health = await provider.health()
        assert health.healthy is True

    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        provider = StubModelProvider()
        await provider.generate(ModelRequest())
        assert len(provider.received_requests) == 1

        provider.reset()
        assert len(provider.received_requests) == 0

    @pytest.mark.asyncio
    async def test_request_no_secret_leak(self):
        """验证请求中的 Secret 不泄漏到响应或日志。"""
        provider = StubModelProvider()
        request = ModelRequest(
            provider_options={"api_key": "sk-secret-123"},
        )
        response = await provider.generate(request)

        # Secret 不在响应中
        r = repr(response)
        assert "sk-secret" not in r
        assert "api_key" not in r
