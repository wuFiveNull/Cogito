"""EchoModelProvider — 回显 Provider，用于离线调试。

将用户最后一条消息原封不动地返回给调用方，
零 Token 消耗，完全兼容 OpenAI 协议的消息格式。

MODEL-ADAPTER / 2-3. ModelRequest / ModelResponse 契约兼容
MODEL-ADAPTER / 5. 流式生成兼容（SSE delta 格式）

用于：
- 离线调试 Agent Loop 的工具调用链
- 验证消息持久化与 Trace 记录
- 不消耗真实模型配额的回归测试

配置方式：
    [model]
    provider = "echo"
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from cogito.model.contracts import (
    ContentPart,
    FinishReason,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Usage,
)
from cogito.model.provider import HealthStatus, ModelProvider


class EchoModelProvider(ModelProvider):
    """回显 Provider —— 将用户最后一条消息原样返回。

    行为：
    - generate / stream 均查找 messages 中最后一个 role=user 的 content
    - 若无 user 消息则返回空文本（finish_reason=stop）
    - 不执行任何网络请求，latency_ms=0
    - capabilities 声明支持 streaming/tools（Agent Loop 不会调用工具，仅回显）
    - 对 response_schema / tools 等字段忽略（回显模式不解析结构化输出）
    """

    def __init__(self, model_id: str = "echo-model") -> None:
        self._model_id = model_id

    @staticmethod
    def _extract_user_text(request: ModelRequest) -> str:
        """从消息列表中提取最后一个 user 消息的文本内容。

        支持两种 content 格式：
        - 纯字符串：{"role":"user", "content":"你好"}
        - 内容块列表：{"role":"user", "content":[{"type":"text","text":"你好"}]}
        """
        for msg in reversed(request.messages):
            role = msg.get("role")
            if role != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                pieces: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        pieces.append(str(part.get("text", "")))
                    elif isinstance(part, str):
                        pieces.append(part)
                return "".join(pieces)
            if content is None:
                continue
            return str(content)
        return ""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """非流式回显：返回完整用户消息。"""
        text = self._extract_user_text(request)
        return ModelResponse(
            request_id=request.request_id,
            provider_request_id=uuid.uuid4().hex,
            model_id=self._model_id,
            content_parts=(ContentPart(part_type="text", text=text),),
            tool_calls=(),
            finish_reason=FinishReason.stop,
            usage=Usage(input_tokens=0, output_tokens=0),
            latency_ms=0,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponse]:
        """流式回显：按字符分块 yield delta，模拟 SSE token 流。

        末帧 finish_reason=stop 以匹配 OpenAI stream 终止语义。
        """
        text = self._extract_user_text(request)
        if not text:
            yield ModelResponse(
                request_id=request.request_id,
                provider_request_id=uuid.uuid4().hex,
                model_id=self._model_id,
                content_parts=(ContentPart(part_type="text", text=""),),
                tool_calls=(),
                finish_reason=FinishReason.stop,
                usage=Usage(input_tokens=0, output_tokens=0),
                latency_ms=0,
            )
            return

        # 逐字符 yield，模拟 token 流（避免大块文本一次性返回）
        for i, ch in enumerate(text):
            is_last = (i == len(text) - 1)
            yield ModelResponse(
                request_id=request.request_id,
                provider_request_id=uuid.uuid4().hex,
                model_id=self._model_id,
                content_parts=(ContentPart(part_type="text", text=ch),),
                tool_calls=(),
                finish_reason=FinishReason.stop if is_last else FinishReason.stop,
                usage=Usage(input_tokens=0, output_tokens=0),
                latency_ms=0,
            )

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            context_window=128_000,
            max_output_tokens=4096,
            modalities=("text",),
            supports_streaming=True,
            supports_tools=True,
            supports_parallel_tools=True,
            supports_json_schema=False,
            supports_prompt_cache=False,
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, latency_ms=1, message="echo: always healthy")
