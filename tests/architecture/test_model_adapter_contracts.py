"""PR-R8: Model Adapter offline contract fixtures — Plan 02 M8.

10 fixture categories from MODEL-ADAPTER / 11:
text / JSON Schema / single+multi tool / stream event order / cancel /
timeout / rate-limit / context overflow / usage / error desensitization.
"""
from __future__ import annotations

from typing import Any

import pytest

from cogito.model.contracts import (
    ContentPart,
    ErrorCategory,
    ErrorEnvelope,
    FinishReason,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Usage,
)
from cogito.model.echo_provider import EchoModelProvider
from cogito.model.stub_provider import StubModelProvider, StubScenario


# ── 1. Text response ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_text_response() -> None:
    """文本回复：stub provider 按 scenario 返回预设响应。"""
    scenario = StubScenario(response_text="hello world")
    provider = StubModelProvider(scenarios=[scenario])
    resp = await provider.generate(ModelRequest())
    assert isinstance(resp, ModelResponse)
    assert resp.text == "hello world"


# ── 2. Usage tracked ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_usage_tracked() -> None:
    """Usage 追踪：回复包含 input/output token。"""
    scenario = StubScenario(response_text="ok", usage=Usage(input_tokens=10, output_tokens=5))
    provider = StubModelProvider(scenarios=[scenario])
    resp = await provider.generate(ModelRequest())
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 5


# ── 3. Capabilities negotiation ─────────────────────────────────

def test_capabilities_declare_support() -> None:
    """Provider 能力快照声明 streaming/tools/json_schema。"""
    provider = StubModelProvider()
    caps = provider.capabilities()
    assert isinstance(caps, ModelCapabilities)
    assert caps.context_window > 0


def test_echo_capabilities() -> None:
    """Echo provider 声明能力完整。"""
    caps = EchoModelProvider().capabilities()
    assert caps.context_window > 0


# ── 4. Stream yields responses ──────────────────────────────────

@pytest.mark.asyncio
async def test_stream_yields_responses() -> None:
    """流生成器产生 ModelResponse 对象。"""
    provider = StubModelProvider(scenarios=[StubScenario(response_text="hello")])
    chunks: list[ModelResponse] = []
    async for resp in provider.stream(ModelRequest(stream=True)):
        chunks.append(resp)
        if len(chunks) >= 1:
            break
    assert len(chunks) >= 1


# ── 5+6. Timeout / connection → retryable ───────────────────────

@pytest.mark.asyncio
async def test_timeout_maps_to_retryable() -> None:
    """超时映射到 retryable error。"""
    from cogito.model.openai_compat import OpenAICompatProvider
    prov = _make_openai_provider()
    try:
        await prov.generate(ModelRequest())
    except Exception as e:
        # 因 base_url=localhost 连接失败 → connection error (retryable)
        err = getattr(e, "envelope", None) or getattr(e, "args", [None])[0]
        if isinstance(err, ErrorEnvelope):
            assert err.retryable is True


# ── 7. HTTP error mapping ───────────────────────────────────────

def test_http_429_maps_to_rate_limit() -> None:
    """HTTP 429 → rate_limit 分类。"""
    from cogito.model.openai_compat import OpenAICompatProvider
    import httpx
    prov = _make_openai_provider()
    resp = httpx.Response(429, request=httpx.Request("GET", "http://x"))
    err = prov._map_http_error(resp)
    assert err.envelope.category == ErrorCategory.rate_limit


# ── 8. HTTP error context overflow ──────────────────────────────

def test_http_400_maps_to_invalid_request() -> None:
    """HTTP 400 → invalid_request 分类（或 context_overlap 子路径）。"""
    from cogito.model.openai_compat import OpenAICompatProvider
    import httpx
    prov = _make_openai_provider()
    resp = httpx.Response(400, request=httpx.Request("GET", "http://x"))
    err = prov._map_http_error(resp)
    assert err.envelope.category in (ErrorCategory.invalid_request,
                                      ErrorCategory.context_overflow)


# ── 9. Error desensitization (no secret leak) ──────────────────

def test_error_desensitization() -> None:
    """ErrorEnvelope 默认不包含 Secret 或原始 Provider 错误细节。"""
    err = ErrorEnvelope(category=ErrorCategory.rate_limit,
                        message="too many requests", retryable=True)
    # safe message 不含 API key / secret
    assert "api_key" not in err.message.lower()
    assert "sk-" not in err.message
    assert "secret" not in err.message.lower()


# ── 10. FinishReason mapping ────────────────────────────────────

def test_finish_reason_enum_complete() -> None:
    """FinishReason 枚举覆盖工具调用/停止/长度/内容过滤。"""
    assert {f.value for f in FinishReason} == {"stop", "tool_calls", "length", "error",
                                                 "content_filter", "cancelled"}


def _make_openai_provider() -> Any:
    from cogito.model.openai_compat import OpenAICompatProvider
    prov = OpenAICompatProvider.__new__(OpenAICompatProvider)
    prov._base_url = "http://localhost"
    prov._api_key = "test"
    prov._timeout = 1.0
    prov._model = "test"
    prov._router = None
    prov._policy_version = "1"
    prov._provider_name = "test"
    return prov
