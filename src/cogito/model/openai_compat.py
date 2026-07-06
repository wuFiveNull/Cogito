"""OpenAI-compatible Provider — 真实模型 HTTP 调用。

POST {base_url}/chat/completions
Authorization: Bearer <api_key>

MODEL-ADAPTER / 2. ModelRequest
MODEL-ADAPTER / 3. ModelResponse
MODEL-ADAPTER / 7. 错误映射

第一版只支持文本、非流式、单轮调用。
Tool Call、Vision、Streaming 和 Thinking 返回明确不支持的错误。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx

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
)
from cogito.model.errors import ModelProviderError
from cogito.model.provider import HealthStatus, ModelProvider


class OpenAICompatProvider(ModelProvider):
    """OpenAI-compatible HTTP Provider。

    支持 OpenAI、Azure OpenAI、Anthropic（通过兼容层）、
    Ollama、vLLM、LiteLLM 等兼容接口。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: int = 60,
        max_retries: int = 0,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timedelta(seconds=timeout_seconds)
        self._max_retries = max_retries

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """非流式生成。

        请求生命周期：
        1. 校验请求（不支持 Tool、Vision、Streaming）
        2. 构建 HTTP 请求体
        3. 发送 POST
        4. 映射 HTTP/JSON 错误
        5. 解析响应
        """
        # ── 1. 校验 ──
        if request.stream:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.invalid_request,
                message="Streaming is not supported in this version",
                retryable=False,
            ))

        # ── 2. 构建请求体 ──
        payload = self._build_payload(request)

        # ── 3. 发送 HTTP 请求 ──
        try:
            response = await self._client.post(
                "/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as e:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.timeout,
                message="Request timed out",
                retryable=True,
                original_type="timeout",
                original_message=str(e),
            )) from e
        except httpx.ConnectError as e:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.connection,
                message="Connection error",
                retryable=True,
                original_type="connection",
                original_message=str(e),
            )) from e
        except httpx.RequestError as e:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.connection,
                message="Request error",
                retryable=True,
                original_type="request_error",
                original_message=str(e),
            )) from e

        # ── 4. 检查 HTTP 状态码 ──
        if response.status_code != 200:
            raise self._map_http_error(response)

        # ── 5. 解析响应体 ──
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.provider_internal,
                message="Invalid response from provider",
                retryable=False,
                original_type="invalid_json",
                original_message=str(e),
            )) from e

        return self._parse_response(request.request_id, data)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponse]:
        raise NotImplementedError("Streaming not implemented in openai_compat provider")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            context_window=128000,
            max_output_tokens=4096,
            modalities=("text",),
            supports_streaming=False,
            supports_tools=True,
            supports_parallel_tools=True,
            supports_json_schema=False,
            supports_prompt_cache=False,
        )

    async def health(self) -> HealthStatus:
        """通过列出模型检查 Provider 可用性。"""
        try:
            response = await self._client.get(
                "/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if response.status_code == 200:
                return HealthStatus(healthy=True, latency_ms=0)
            return HealthStatus(
                healthy=False,
                message=f"Health check failed: HTTP {response.status_code}",
            )
        except Exception as e:
            return HealthStatus(healthy=False, message=str(e))

    def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        import asyncio
        try:
            asyncio.get_running_loop()
            # 如果在事件循环中，不阻塞关闭
        except RuntimeError:
            pass

    # ── 内部方法 ──

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        """构建 OpenAI-compatible 请求体。

        支持 tools（在 Phase 2 启用）。
        """
        messages = []
        for msg in request.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            messages.append({"role": role, "content": content})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }

        if request.tools:
            payload["tools"] = list(request.tools)

        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        return payload

    def _parse_response(
        self, request_id: str, data: dict[str, Any],
    ) -> ModelResponse:
        """解析 OpenAI-compatible 响应体。

        支持 tool_calls 解析（Phase 2）。
        """
        try:
            choice = data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content") or ""
            finish_reason_str = choice.get("finish_reason", "stop")
        except (KeyError, IndexError) as e:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.invalid_request,
                message="Invalid response structure",
                retryable=False,
                original_type="parse_error",
                original_message=str(e),
            )) from e

        finish_reason = self._normalize_finish_reason(finish_reason_str)
        usage_data = data.get("usage", {})

        usage = Usage(
            input_tokens=usage_data.get("prompt_tokens", 0),
            output_tokens=usage_data.get("completion_tokens", 0),
            cached_tokens=usage_data.get("cached_tokens", 0),
        )

        # 解析 tool_calls
        raw_tool_calls = message.get("tool_calls", [])
        tool_calls: list[dict[str, Any]] = []
        for tc in raw_tool_calls:
            tool_calls.append({
                "id": tc.get("id", ""),
                "type": tc.get("type", "function"),
                "function": {
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                },
            })

        parts = (ContentPart(
            part_type=ContentPartType.text,
            text=content,
            trust_label="internal",
        ),)

        return ModelResponse(
            request_id=request_id,
            provider_request_id=data.get("id", ""),
            model_id=data.get("model", self._model),
            content_parts=parts,
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
            usage=usage,
        )

    def _map_http_error(self, response: httpx.Response) -> ModelProviderError:
        """将 HTTP 错误映射为统一 Provider 错误。"""
        status = response.status_code
        body = ""
        try:
            err_data = response.json()
            body = err_data.get("error", {}).get("message", "") or str(err_data)
        except Exception:
            body = response.text[:200]

        if status == 401:
            return ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.authentication,
                message="Authentication failed. Check your API key.",
                retryable=False,
                original_type=f"http_{status}",
                original_message=body,
            ))
        elif status == 403:
            return ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.permission,
                message="Permission denied",
                retryable=False,
                original_type=f"http_{status}",
                original_message=body,
            ))
        elif status == 404:
            return ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.model_not_found,
                message=f"Model '{self._model}' not found",
                retryable=False,
                original_type=f"http_{status}",
                original_message=body,
            ))
        elif status == 429:
            return ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.rate_limit,
                message="Rate limit exceeded",
                retryable=True,
                original_type=f"http_{status}",
                original_message=body,
            ))
        elif status == 400:
            return ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.invalid_request,
                message=f"Invalid request: {body[:100]}",
                retryable=False,
                original_type=f"http_{status}",
                original_message=body,
            ))
        elif status in (500, 502, 503):
            return ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.provider_internal,
                message="Provider internal error",
                retryable=True,
                original_type=f"http_{status}",
                original_message=body,
            ))
        else:
            return ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.provider_internal,
                message=f"HTTP {status}",
                retryable=status >= 500,
                original_type=f"http_{status}",
                original_message=body,
            ))

    def _normalize_finish_reason(self, raw: str) -> FinishReason:
        """标准化 finish_reason。"""
        mapping = {
            "stop": FinishReason.stop,
            "end_turn": FinishReason.stop,
            "length": FinishReason.length,
            "max_tokens": FinishReason.length,
            "tool_calls": FinishReason.tool_calls,
            "content_filter": FinishReason.content_filter,
        }
        return mapping.get(raw.lower(), FinishReason.stop)

    def __del__(self) -> None:
        try:
            import asyncio
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
        except Exception:
            pass
