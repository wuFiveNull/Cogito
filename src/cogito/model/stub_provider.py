"""StubModelProvider — 确定性测试 Provider。

提供固定响应、预设序列和错误模拟能力。
所有测试应使用 Stub Provider，不依赖在线模型。

Phase 2: 支持工具调用模拟。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from cogito.model.contracts import (
    ContentPart,
    ErrorEnvelope,
    FinishReason,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Usage,
)
from cogito.model.errors import ModelProviderError
from cogito.model.provider import HealthStatus, ModelProvider


class StubScenario:
    """Stub Provider 的预设行为。

    tool_calls: 预设的工具调用响应列表。
        当 finish_reason=tool_calls 且 tool_calls 非空时，
        返回预设的 tool_calls。
    """

    def __init__(
        self,
        response_text: str = "This is a stub response.",
        finish_reason: FinishReason = FinishReason.stop,
        usage: Usage | None = None,
        latency_ms: int = 50,
        error: ErrorEnvelope | None = None,
        invalid_output: bool = False,
        tool_calls: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.response_text = response_text
        self.finish_reason = finish_reason
        self.usage = usage or Usage(input_tokens=10, output_tokens=5)
        self.latency_ms = latency_ms
        self.error = error
        self.invalid_output = invalid_output
        self.tool_calls = tool_calls


class StubModelProvider(ModelProvider):
    """确定性测试 Provider。

    - 固定 FinalResponse
    - 多次调用返回预设序列
    - 支持模拟 timeout、rate_limit 等错误
    - 记录收到的请求（便于断言）
    - 支持工具调用模拟（Phase 2）
    """

    def __init__(self, scenarios: list[StubScenario] | None = None) -> None:
        self._scenarios = scenarios or [StubScenario()]
        self._call_index = 0
        self.received_requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.received_requests.append(request)

        scenario = self._scenarios[self._call_index % len(self._scenarios)]
        self._call_index += 1

        if scenario.error:
            raise ModelProviderError(scenario.error)

        parts = (ContentPart(part_type="text", text=scenario.response_text),)

        # 工具调用
        tool_calls = ()
        if scenario.finish_reason == FinishReason.tool_calls:
            tool_calls = scenario.tool_calls

        return ModelResponse(
            request_id=request.request_id,
            provider_request_id=uuid.uuid4().hex,
            model_id="stub-model",
            content_parts=parts,
            tool_calls=tool_calls,
            finish_reason=scenario.finish_reason,
            usage=scenario.usage,
            latency_ms=scenario.latency_ms,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponse]:
        """流式生成（测试用）—— 把预设文本按词分块 yield delta。

        每个 delta 为一段增量文本；末帧 finish_reason=stop。
        消费一个 scenario（与 generate 相同的计数逻辑）。
        """
        scenario = self._scenarios[self._call_index % len(self._scenarios)]
        self._call_index += 1
        self.received_requests.append(request)

        if scenario.error:
            raise ModelProviderError(scenario.error)

        # 按词分块，保留词间空格，模拟 token 流
        text = scenario.response_text
        chunks = text.split(" ") if text else [""]
        for i, word in enumerate(chunks):
            piece = word + (" " if i < len(chunks) - 1 else "")
            if not piece:
                continue
            yield ModelResponse(
                request_id=request.request_id,
                provider_request_id=uuid.uuid4().hex,
                model_id="stub-model",
                content_parts=(ContentPart(part_type="text", text=piece),),
                tool_calls=(),
                finish_reason=FinishReason.stop,
                usage=Usage(input_tokens=0, output_tokens=0),
            )

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            context_window=128000,
            max_output_tokens=4096,
            modalities=("text",),
            supports_streaming=True,
            supports_tools=True,
            supports_parallel_tools=True,
            supports_json_schema=False,
            supports_prompt_cache=False,
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, latency_ms=5)

    def reset(self) -> None:
        """重置调用计数和接收请求记录。"""
        self._call_index = 0
        self.received_requests.clear()
