"""ModelProvider Protocol — 统一模型提供商接口。

AGENT-COGNITION / 4. Model 与 Embedding Provider
MODEL-ADAPTER / 2. ModelRequest
MODEL-ADAPTER / 3. ModelResponse
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from cogito.model.contracts import (
    ErrorEnvelope,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
)


class HealthStatus:
    """Provider 健康状态。"""
    healthy: bool = True
    latency_ms: int = 0
    message: str = ""

    def __init__(self, healthy: bool = True, latency_ms: int = 0, message: str = "") -> None:
        self.healthy = healthy
        self.latency_ms = latency_ms
        self.message = message


class ModelProvider(Protocol):
    """模型提供商统一协议。

    实现者必须：
    - 不修改传入的 ModelRequest
    - 不泄漏 API Key 或 Secret 到返回的响应中
    - 错误通过 ErrorEnvelope 传递
    """

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """非流式文本生成。"""
        ...

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponse]:
        """流式生成，每个事件包含增量内容。"""
        ...  # pragma: no cover
        if False:
            yield  # make async generator

    def capabilities(self) -> ModelCapabilities:
        """返回 Provider 能力声明。"""
        ...

    async def health(self) -> HealthStatus:
        """返回 Provider 健康状态。"""
        ...
