"""EchoModelProvider 测试 —— 验证回显逻辑与流式输出。"""

from __future__ import annotations

import pytest

from cogito.model.contracts import FinishReason, ModelRequest
from cogito.model.echo_provider import EchoModelProvider


@pytest.fixture
def provider() -> EchoModelProvider:
    return EchoModelProvider()


# ── generate ──


@pytest.mark.asyncio
async def test_echo_simple_user_message(provider: EchoModelProvider) -> None:
    req = ModelRequest(messages=({"role": "user", "content": "你好"},))
    resp = await provider.generate(req)
    assert resp.text == "你好"
    assert resp.finish_reason == FinishReason.stop
    assert resp.usage.total_tokens == 0
    assert resp.request_id == req.request_id


@pytest.mark.asyncio
async def test_echo_ignores_system_uses_last_user(provider: EchoModelProvider) -> None:
    req = ModelRequest(messages=(
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "回复1"},
        {"role": "user", "content": "第二个问题"},
    ))
    resp = await provider.generate(req)
    assert resp.text == "第二个问题"


@pytest.mark.asyncio
async def test_echo_content_block_format(provider: EchoModelProvider) -> None:
    req = ModelRequest(messages=(
        {"role": "user", "content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]},
    ))
    resp = await provider.generate(req)
    assert resp.text == "hello world"


@pytest.mark.asyncio
async def test_echo_empty_messages(provider: EchoModelProvider) -> None:
    req = ModelRequest(messages=())
    resp = await provider.generate(req)
    assert resp.text == ""
    assert resp.finish_reason == FinishReason.stop


@pytest.mark.asyncio
async def test_echo_no_user_message(provider: EchoModelProvider) -> None:
    req = ModelRequest(messages=(
        {"role": "system", "content": "sysonly"},
        {"role": "assistant", "content": "asst reply"},
    ))
    resp = await provider.generate(req)
    assert resp.text == ""


# ── stream ──


@pytest.mark.asyncio
async def test_echo_stream_reconstructs_text(provider: EchoModelProvider) -> None:
    req = ModelRequest(messages=({"role": "user", "content": "hello"},))
    chunks: list[str] = []
    async for r in provider.stream(req):
        chunks.append(r.text)
    assert "".join(chunks) == "hello"


@pytest.mark.asyncio
async def test_echo_stream_empty(provider: EchoModelProvider) -> None:
    req = ModelRequest(messages=())
    frames = [r async for r in provider.stream(req)]
    # 至少有一帧（空 content，stop）
    assert len(frames) >= 1
    assert frames[-1].finish_reason == FinishReason.stop


@pytest.mark.asyncio
async def test_echo_stream_single_char(provider: EchoModelProvider) -> None:
    """单字符消息应当只产生一帧。"""
    req = ModelRequest(messages=({"role": "user", "content": "x"},))
    frames = [r async for r in provider.stream(req)]
    assert len(frames) == 1
    assert frames[0].text == "x"


# ── capabilities & health ──


@pytest.mark.asyncio
async def test_echo_capabilities(provider: EchoModelProvider) -> None:
    cap = provider.capabilities()
    assert cap.supports_streaming is True
    assert cap.supports_tools is True
    assert cap.context_window == 128_000


@pytest.mark.asyncio
async def test_echo_health(provider: EchoModelProvider) -> None:
    h = await provider.health()
    assert h.healthy is True
