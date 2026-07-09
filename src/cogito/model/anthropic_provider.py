"""AnthropicProvider — Claude 原生 Messages API 适配器。

POST {base_url}/v1/messages
Headers: x-api-key, anthropic-version

与 OpenAPI 兼容层的关键差异（由此适配器收敛）：
- system prompt 是顶层字段，不在 messages 数组中
- 消息内容使用 content block：text / image(source=base46|url) / tool_use / tool_result
- 工具 Schema 是扁平的 {name, description, input_schema}，而非 OpenAI 的 function 包装
- 流式事件不是增量 choice，而是 message_start/content_block_delta/message_delta 等
- 错误体为 {error:{type, message}}

MODEL-ADAPTER / 2. ModelRequest（以 OpenAI 风格 content block 为通用交换格式）
MODEL-ADAPTER / 4. Vision：图片以 data: URL 或 http URL 传入，转 Anthropic source
MODEL-ADAPTER / 7. 错误映射
"""

from __future__ import annotations

import json
import re
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

# Anthropic 工具名规则：字母、数字、下划线、连字符，最长 64
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_ANTHROPIC_VERSION = "2023-06-01"

# Anthropic stop_reason → FinishReason
_STOP_REASON_MAP = {
    "end_turn": FinishReason.stop,
    "stop_sequence": FinishReason.stop,
    "max_tokens": FinishReason.length,
    "tool_use": FinishReason.tool_calls,
    "pause_turn": FinishReason.stop,
    "eta": FinishReason.stop,
}

# Anthropic 错误类型 → ErrorCategory
_ERROR_TYPE_MAP = {
    "invalid_request_error": ErrorCategory.invalid_request,
    "authentication_error": ErrorCategory.authentication,
    "permission_error": ErrorCategory.permission,
    "not_found_error": ErrorCategory.model_not_found,
    "rate_limit_error": ErrorCategory.rate_limit,
    "request_too_large_error": ErrorCategory.invalid_request,
    "api_error": ErrorCategory.provider_internal,
    "overloaded_error": ErrorCategory.provider_internal,
}


class AnthropicProvider(ModelProvider):
    """Claude 原生 Messages API 适配器。

    以 OpenAI 风格 content block（text / image_url）作为 ModelRequest 中的
    通用交换格式，在此适配器内部转换为 Anthropic 原生 content block。
    """

    HEALTH_CACHE_TTL_S = 30

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: int = 60,
        max_retries: int = 0,
        *,
        context_window: int = 200_000,
        max_output_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timedelta(seconds=timeout_seconds)
        self._max_retries = max_retries

        self._context_window = context_window
        self._max_output_tokens = max_output_tokens

        # 名称可逆映射：Anthropic 安全名 → 原始规范名（处理超长/非法字符工具名）
        self._tool_name_map: dict[str, str] = {}

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        )

        self._health_cache: tuple[HealthStatus, float] | None = None

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if request.stream:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.invalid_request,
                message="Streaming is not supported in generate(); use stream()",
                retryable=False,
            ))

        payload = self._build_payload(request)

        try:
            response = await self._client.post(
                "/v1/messages",
                json=payload,
                headers=self._auth_headers(),
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

        if response.status_code != 200:
            raise self._map_error(response)

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

        return self._parse_response(request.request_id, data, request=request)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponse]:
        """流式生成 —— 解析 Anthropic SSE 事件序列。

        事件类型：message_start → content_block_start → content_block_delta →
        content_block_stop → message_delta → message_stop（ping 忽略）。
        """
        if not request.stream:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.invalid_request,
                message="stream() requires request.stream=True",
                retryable=False,
            ))

        payload = self._build_payload(request, stream=True)
        headers = self._auth_headers()
        headers["Accept"] = "text/event-stream"

        # 工具调用流式累积：index → {id, name, input_json}
        tool_blocks: dict[int, dict[str, Any]] = {}
        emitted_tool = False

        try:
            async with self._client.stream(
                "POST",
                "/v1/messages",
                json=payload,
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    raise self._map_error(response)

                request_id = request.request_id
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if not data_str:
                        continue
                    try:
                        event = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    etype = event.get("type", "")

                    if etype == "ping":
                        continue

                    elif etype == "message_start":
                        # 可在此提取初始 usage；首帧可 yield 空文本
                        continue

                    elif etype == "content_block_start":
                        block = event.get("content_block", {})
                        index = event.get("index", 0)
                        if block.get("type") == "tool_use":
                            tool_blocks[index] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input_json": "",
                            }

                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        index = event.get("index", 0)
                        dtype = delta.get("type", "")
                        if dtype == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                yield ModelResponse(
                                    request_id=request_id,
                                    provider_request_id="",
                                    model_id=self._model,
                                    content_parts=(ContentPart(
                                        part_type=ContentPartType.text,
                                        text=text,
                                        trust_label="internal",
                                    ),),
                                    tool_calls=(),
                                    finish_reason=FinishReason.stop,
                                    usage=Usage(),
                                )
                        elif dtype == "input_json_delta":
                            tb = tool_blocks.get(index)
                            if tb is not None:
                                tb["input_json"] += delta.get("partial_json", "")

                    elif etype in ("content_block_stop", "message_delta"):
                        # message_delta 携带 stop_reason 和 output usage
                        if etype == "message_delta":
                            mdelta = event.get("delta", {})
                            stop_reason_str = mdelta.get("stop_reason", "end_turn")
                            finish = _normalize_stop_reason(stop_reason_str)
                            # 当最终是 tool_use 时，yield 一个携带 tool_calls 的帧
                            tool_blocks_ready = (
                                finish == FinishReason.tool_calls
                                and tool_blocks
                                and not emitted_tool
                            )
                            if tool_blocks_ready:
                                emitted_tool = True
                                calls = tuple(
                                    {
                                        "id": tb["id"],
                                        "type": "function",
                                        "function": {
                                            "name": self._tool_name_map.get(
                                                tb["name"], tb["name"],
                                            ),
                                            "arguments": tb["input_json"] or "{}",
                                        },
                                    }
                                    for tb in tool_blocks.values()
                                )
                                yield ModelResponse(
                                    request_id=request_id,
                                    provider_request_id="",
                                    model_id=self._model,
                                    content_parts=(),
                                    tool_calls=calls,
                                    finish_reason=finish,
                                    usage=Usage(),
                                )

                    elif etype == "message_stop":
                        break

        except httpx.TimeoutException as e:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.timeout,
                message="Stream timed out",
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

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            context_window=self._context_window,
            max_output_tokens=self._max_output_tokens,
            modalities=("text", "image"),
            supports_streaming=True,
            supports_tools=True,
            supports_parallel_tools=True,
            supports_json_schema=True,
            supports_prompt_cache=True,
        )

    async def health(self) -> HealthStatus:
        import time
        now = time.monotonic()
        if self._health_cache is not None and now - self._health_cache[1] < self.HEALTH_CACHE_TTL_S:
            return self._health_cache[0]

        try:
            # 用一个极小请求探测可用性：max_tokens=1 的 messages 调用
            response = await self._client.post(
                "/v1/messages",
                json={
                    "model": self._model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers=self._auth_headers(),
            )
            if response.status_code == 200:
                status = HealthStatus(healthy=True, latency_ms=0)
            else:
                body = ""
                try:
                    body = response.text[:200]
                except Exception:
                    pass
                status = HealthStatus(
                    healthy=False,
                    message=f"Health check failed: HTTP {response.status_code} {body}",
                )
        except Exception as e:
            status = HealthStatus(healthy=False, message=str(e))

        self._health_cache = (status, now)
        return status

    def close(self) -> None:
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass

    # ── 内部方法 ──

    def _auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    def _build_payload(self, request: ModelRequest, stream: bool = False) -> dict[str, Any]:
        """构建 Anthropic Messages 请求体。

        - 提取 role=system 消息 → 顶层 system
        - 转换 content block（text / image_url → Anthropic source / tool_result）
        - 转换 OpenAI 工具 Schema → Anthropic {name, description, input_schema}
        """
        self._tool_name_map = {}

        system_blocks: list[Any] = []
        messages: list[dict[str, Any]] = []

        for msg in request.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                # 系统提示词提取为顶层字段
                if isinstance(content, list):
                    for block in content:
                        converted = _to_anthropic_content_block(block)
                        if converted is not None:
                            system_blocks.append(converted)
                else:
                    system_blocks.append({"type": "text", "text": content or ""})
                continue

            if role == "tool":
                # OpenAI role=tool → Anthropic role=user + tool_result
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": content or "",
                    }],
                })
                continue

            # user / assistant：转换 content block
            if isinstance(content, list):
                blocks = [_to_anthropic_content_block(b) for b in content]
                blocks = [b for b in blocks if b is not None]
                anthropic_content: Any = blocks if blocks else ""
            else:
                anthropic_content = content or ""

            entry: dict[str, Any] = {"role": role, "content": anthropic_content}

            # assistant 含 tool_calls：转 Anthropic tool_use block
            tool_calls = msg.get("tool_calls")
            if role == "assistant" and tool_calls:
                blocks = []
                if isinstance(anthropic_content, list):
                    blocks = anthropic_content
                elif anthropic_content:
                    blocks = [{"type": "text", "text": anthropic_content}]
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    raw_name = fn.get("name", "")
                    safe_name = _safe_tool_name(raw_name)
                    if safe_name != raw_name:
                        self._tool_name_map[safe_name] = raw_name
                    try:
                        inp = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        inp = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": safe_name,
                        "input": inp,
                    })
                entry["content"] = blocks

            messages.append(entry)

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_output_tokens or self._max_output_tokens,
            "messages": messages,
            "stream": stream,
        }

        if system_blocks:
            # 单 block 用字符串，多 block 用列表
            payload["system"] = system_blocks[0] if len(system_blocks) == 1 else system_blocks

        if request.tools:
            payload["tools"] = [
                _openai_tool_to_anthropic(t, self._tool_name_map) for t in request.tools
            ]

        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop:
            payload["stop_sequences"] = list(request.stop)

        # 结构化输出：Anthropic 无原生 json_schema，使用工具约束输出 JSON
        self._apply_response_format(payload, request)

        return payload

    def _apply_response_format(
        self, payload: dict[str, Any], request: ModelRequest,
    ) -> None:
        """将 response_schema / response_format 映射到 Anthropic 结构化输出。

        Anthropic 没有 OpenAI 的 response_format=json_schema。约定：
        - response_schema 存在时，注入一个具名工具强制模型输出 JSON input
        - response_format == "json" 时同上，使用通用 schema
        """
        schema: dict[str, Any] | None = None
        tool_name = "_response"
        if request.response_schema:
            schema = request.response_schema
            schema_name = request.response_schema.get("name")
            if isinstance(schema_name, str) and schema_name:
                tool_name = _safe_tool_name(schema_name)
        elif request.response_format == "json":
            schema = {
                "type": "object",
                "properties": {"response": {"type": "string"}},
                "required": ["response"],
            }

        if schema is None:
            return

        tools = payload.get("tools", [])
        # 避免重复注入
        existing = [t.get("name") for t in tools]
        if tool_name not in existing:
            tools.append({
                "name": tool_name,
                "description": "Respond with a single JSON object matching the schema.",
                "input_schema": schema,
            })
            payload["tools"] = tools
        payload["tool_choice"] = {"type": "tool", "name": tool_name}

    def _parse_response(
        self, request_id: str, data: dict[str, Any],
        request: ModelRequest | None = None,
    ) -> ModelResponse:
        try:
            content_blocks = data.get("content", [])
            stop_reason_str = data.get("stop_reason", "end_turn")
        except (AttributeError,) as e:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.invalid_request,
                message="Invalid response structure",
                retryable=False,
                original_type="parse_error",
                original_message=str(e),
            )) from e

        finish_reason = _normalize_stop_reason(stop_reason_str)

        # 提取文本与 tool_use
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                raw_name = block.get("name", "")
                canonical_name = self._tool_name_map.get(raw_name, raw_name)
                inp = block.get("input", {})
                is_dict_input = isinstance(inp, dict)
                try:
                    args = json.dumps(inp, ensure_ascii=False) if is_dict_input else str(inp)
                except (TypeError, ValueError):
                    args = "{}"
                if request and request.tools:
                    _revalidate_tool_arguments(canonical_name, args, request.tools)
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {"name": canonical_name, "arguments": args},
                })

        # Usage：含 prompt caching 字段
        usage_data = data.get("usage", {})
        usage = Usage(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
            cached_tokens=usage_data.get("cache_read_input_tokens", 0),
        )

        parts = (ContentPart(
            part_type=ContentPartType.text,
            text="".join(text_parts),
            trust_label="internal",
        ),)

        return ModelResponse(
            request_id=request_id,
            provider_request_id=data.get("id", ""),
            model_id=data.get("model", self._model),
            content_parts=parts if text_parts else (),
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
            usage=usage,
        )

    def _map_error(self, response: httpx.Response) -> ModelProviderError:
        """将 Anthropic 错误体映射为统一 Provider 错误。"""
        status = response.status_code
        body = ""
        error_type = ""
        try:
            err_data = response.json()
            err = err_data.get("error", {})
            error_type = err.get("type", "")
            body = err.get("message", "") or str(err_data)
        except Exception:
            body = response.text[:200]

        # 状态码作为基础分类（强信号）
        if status == 401:
            category = ErrorCategory.authentication
        elif status == 403:
            category = ErrorCategory.permission
        elif status == 404:
            category = ErrorCategory.model_not_found
        elif status == 429:
            category = ErrorCategory.rate_limit
        elif status == 400:
            category = ErrorCategory.invalid_request
        elif status in (500, 502, 503):
            category = ErrorCategory.provider_internal
        else:
            category = ErrorCategory.provider_internal

        # Anthropic 错误类型覆盖为更精确的分类（仅已知类型）
        if error_type in _ERROR_TYPE_MAP:
            category = _ERROR_TYPE_MAP[error_type]

        retryable = category in (
            ErrorCategory.rate_limit,
            ErrorCategory.timeout,
            ErrorCategory.connection,
            ErrorCategory.provider_internal,
        )
        retry_after = None
        if category == ErrorCategory.rate_limit:
            retry_after = timedelta(seconds=30)
        elif category == ErrorCategory.provider_internal:
            retry_after = timedelta(seconds=5)

        messages = {
            ErrorCategory.authentication: "Authentication failed. Check your API key.",
            ErrorCategory.permission: "Permission denied",
            ErrorCategory.model_not_found: f"Model '{self._model}' not found",
            ErrorCategory.rate_limit: "Rate limit exceeded",
            ErrorCategory.invalid_request: "Invalid request",
            ErrorCategory.provider_internal: "Provider internal error",
        }
        message = messages.get(category, f"Anthropic error: {error_type}")

        return ModelProviderError(ErrorEnvelope(
            category=category,
            message=message,
            retryable=retryable,
            retry_after=retry_after,
            original_type=error_type or f"http_{status}",
            original_message=body[:500],
        ))


# ── 模块级辅助函数 ──────────────────────────────────────────────────────────


def _normalize_stop_reason(raw: str) -> FinishReason:
    return _STOP_REASON_MAP.get(raw.lower(), FinishReason.stop)


def _safe_tool_name(name: str) -> str:
    """将工具名约束为 Anthropic 允许的字符集。"""
    if not name:
        return "_tool"
    if _TOOL_NAME_RE.match(name):
        return name
    # 替换非法字符为下划线，截断到 64
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]
    return safe or "_tool"


def _to_anthropic_content_block(block: Any) -> dict[str, Any] | None:
    """将 OpenAI 风格 content block 转换为 Anthropic content block。

    输入格式（通用交换格式）：
    - {"type":"text", "text":"..."}
    - {"type":"image_url", "image_url":{"url":"data:image/png;base64,..." 或 "https://..."}}
    已是 Anthropic 格式的块（含 "source"）也会被透传。
    """
    if not isinstance(block, dict):
        return None
    btype = block.get("type", "")

    if btype == "text":
        return {"type": "text", "text": str(block.get("text", ""))}

    # 已是 Anthropic 原生格式：透传
    if btype == "image" and "source" in block:
        return block  # type: ignore[return-value]
    if btype in ("tool_use", "tool_result"):
        return block  # type: ignore[return-value]

    if btype == "image_url":
        url_obj = block.get("image_url", {})
        url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj)
        if url.startswith("data:"):
            try:
                header, data = url.split(",", 1)
                media_type = header.split(";")[0].split(":")[1]
            except (ValueError, IndexError):
                return None
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        if url.startswith("http://") or url.startswith("https://"):
            return {"type": "image", "source": {"type": "url", "url": url}}
        return None

    return None


def _openai_tool_to_anthropic(
    tool: dict[str, Any], name_map: dict[str, str],
) -> dict[str, Any]:
    """将 OpenAI 函数工具 Schema 转为 Anthropic 工具 Schema。

    OpenAI: {"type":"function", "function":{"name","description","parameters"}}
    Anthropic: {"name","description","input_schema"}
    """
    fn = tool.get("function", {})
    raw_name = fn.get("name", "")
    safe_name = _safe_tool_name(raw_name)
    return {
        "name": safe_name,
        "description": str(fn.get("description", ""))[:512],
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _revalidate_tool_arguments(
    tool_name: str,
    raw_arguments: str,
    tool_schemas: tuple[dict[str, Any], ...],
) -> None:
    """用请求中的 tool schema 校验返回的 tool_calls 参数（与 OpenAI 适配器一致）。"""
    schema = None
    for t in tool_schemas:
        fn = t.get("function", {})
        if fn.get("name") == tool_name:
            schema = fn.get("parameters", {})
            break

    if not schema:
        return

    try:
        args = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except (json.JSONDecodeError, TypeError):
        raise ModelProviderError(ErrorEnvelope(
            category=ErrorCategory.invalid_request,
            message=f"Tool '{tool_name}': invalid arguments JSON from provider",
            retryable=False,
        ))

    if not isinstance(args, dict):
        return

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for field in required:
        if field not in args:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.invalid_request,
                message=f"Tool '{tool_name}': response missing required field '{field}'",
                retryable=False,
            ))
    for key, value in args.items():
        prop = properties.get(key, {})
        if "enum" in prop and value not in prop["enum"]:
            raise ModelProviderError(ErrorEnvelope(
                category=ErrorCategory.invalid_request,
                message=f"Tool '{tool_name}': response field '{key}' has invalid enum value",
                retryable=False,
            ))
