# Cogito v1 — Provider 与配置系统最终设计

## 1. 设计目标

Provider 层负责把 Cogito 内部统一的模型请求转换为外部厂商 API 请求，并把响应转换回统一格式。

目标：

1. 支持主模型、轻量模型、视觉模型和 Embedding 模型。
2. 支持 OpenAI、DeepSeek、DashScope/Qwen 以及 OpenAI-Compatible 服务。
3. 上层只按逻辑角色调用模型，不直接依赖厂商或具体模型名。
4. 流式和非流式调用共享统一的数据模型。
5. Provider 不读取配置文件、环境变量、Prompt、Session 或 Channel。
6. API Key 只从环境变量读取，不写入 TOML。
7. 配置在 Bootstrap 阶段转换为运行时对象。
8. Provider、Adapter、配置读取和模型路由可以独立测试。

---

## 2. 核心架构

```text
TurnRunner / Phase Pipeline
          │
          │ complete(role, request)
          │ stream(role, request)
          ▼
┌──────────────────────────────┐
│ LLMService                   │
│ 按逻辑角色选择模型            │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ ModelRegistry                │
│ 保存模型实例                  │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ ChatBackend                  │
│ SDK、超时、重试、取消          │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ ProviderAdapter              │
│ 厂商请求与响应协议适配         │
└──────────────┬───────────────┘
               ▼
          外部模型 API
```

Embedding 使用独立路径：

```text
Memory / Deduper
      │
      ▼
EmbeddingService
      │
      ▼
/embeddings
```

---

## 3. 目录结构

```text
cogito/
├── llm/
│   ├── __init__.py
│   ├── service.py
│   ├── registry.py
│   ├── protocol.py
│   ├── backend.py
│   ├── request.py
│   ├── response.py
│   ├── stream.py
│   ├── capabilities.py
│   ├── errors.py
│   ├── embedding.py
│   └── adapters/
│       ├── __init__.py
│       ├── base.py
│       ├── openai.py
│       ├── openai_compatible.py
│       ├── deepseek.py
│       └── dashscope.py
│
├── config/
│   ├── __init__.py
│   ├── schema.py
│   ├── loader.py
│   └── errors.py
│
├── bootstrap/
│   ├── __init__.py
│   ├── providers.py
│   └── application.py
│
├── prompts/
│   └── system.md
├── config.toml
└── __main__.py
```

---

## 4. 请求数据模型

```python
# cogito/llm/request.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence


@dataclass(frozen=True)
class TextContent:
    text: str


@dataclass(frozen=True)
class ImageContent:
    url: str
    detail: Literal["auto", "low", "high"] = "auto"


ContentPart = TextContent | ImageContent
MessageContent = str | Sequence[ContentPart]


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    raw_arguments: str


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: MessageContent | None

    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()

    tool_choice: str | Mapping[str, Any] | None = "auto"

    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: tuple[str, ...] = ()

    disable_thinking: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

不要使用可变默认值：

```python
# 错误
tools: list[dict] = []

# 正确
tools: tuple[ToolDefinition, ...] = ()
```

---

## 5. 响应数据模型

```python
# cogito/llm/response.py

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str

    raw_arguments: str
    arguments: Mapping[str, Any] | None = None
    parse_error: str | None = None


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()

    thinking: str | None = None
    finish_reason: str | None = None

    model: str | None = None
    provider: str | None = None
    provider_response_id: str | None = None

    usage: TokenUsage | None = None
    provider_fields: Mapping[str, Any] = field(default_factory=dict)
```

Tool Call 参数解析失败时，应保留 `raw_arguments`，不要让整个响应解析失败。

---

## 6. 流式事件

```python
# cogito/llm/stream.py

from dataclasses import dataclass


@dataclass(frozen=True)
class ContentDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    call_id_delta: str | None = None
    name_delta: str | None = None
    arguments_delta: str | None = None


@dataclass(frozen=True)
class UsageDelta:
    usage: TokenUsage


@dataclass(frozen=True)
class StreamCompleted:
    finish_reason: str | None = None


LLMStreamEvent = (
    ContentDelta
    | ThinkingDelta
    | ToolCallDelta
    | UsageDelta
    | StreamCompleted
)
```

Provider 应返回 `AsyncIterator[LLMStreamEvent]`，而不是直接调用 Channel callback。

---

## 7. 模型能力

```python
# cogito/llm/capabilities.py

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelCapabilities:
    text: bool = True
    tools: bool = False
    vision: bool = False
    thinking: bool = False
    streaming: bool = True
    embedding: bool = False


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str

    capabilities: ModelCapabilities

    max_output_tokens: int = 4096
    default_extra_body: dict = field(default_factory=dict)
```

调用前校验：

```python
def validate_request_capabilities(
    profile: ModelProfile,
    request: ChatRequest,
) -> None:
    if request.tools and not profile.capabilities.tools:
        raise ModelCapabilityError(
            f"model {profile.name!r} does not support tools"
        )

    if request_contains_image(request):
        if not profile.capabilities.vision:
            raise ModelCapabilityError(
                f"model {profile.name!r} does not support vision"
            )
```

不支持图片时，必须路由到视觉模型或明确报错，不能静默删除图片。

---

## 8. Provider Protocol

```python
# cogito/llm/protocol.py

from collections.abc import AsyncIterator
from typing import Protocol


class ChatProvider(Protocol):
    async def complete(
        self,
        request: ChatRequest,
    ) -> LLMResponse:
        ...

    def stream(
        self,
        request: ChatRequest,
    ) -> AsyncIterator[LLMStreamEvent]:
        ...

    async def close(self) -> None:
        ...
```

---

## 9. ProviderAdapter

```python
# cogito/llm/adapters/base.py

from abc import ABC, abstractmethod
from typing import Any


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    def build_request(
        self,
        profile: ModelProfile,
        request: ChatRequest,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def parse_response(
        self,
        raw_response: Any,
        profile: ModelProfile,
    ) -> LLMResponse:
        ...

    @abstractmethod
    def parse_stream_chunk(
        self,
        chunk: Any,
    ) -> tuple[LLMStreamEvent, ...]:
        ...

    @abstractmethod
    def map_error(
        self,
        exc: Exception,
    ) -> "LLMError":
        ...
```

Adapter 只负责厂商协议差异，不负责配置读取、模型路由、Prompt、Session 或 Channel。

注册表：

```python
# cogito/llm/adapters/__init__.py

ADAPTER_FACTORIES = {
    "openai": OpenAIAdapter,
    "openai_compatible": OpenAICompatibleAdapter,
    "deepseek": DeepSeekAdapter,
    "dashscope": DashScopeAdapter,
}
```

配置中必须显式声明 Adapter，不通过模型名或 Base URL 猜测。

---

## 10. OpenAI-Compatible Adapter

```python
class OpenAICompatibleAdapter(ProviderAdapter):
    name = "openai_compatible"

    def build_request(
        self,
        profile: ModelProfile,
        request: ChatRequest,
        *,
        stream: bool,
    ) -> dict:
        validate_request_capabilities(profile, request)

        payload = {
            "model": profile.model,
            "messages": [
                self._serialize_message(message)
                for message in request.messages
            ],
            "stream": stream,
        }

        max_tokens = (
            request.max_output_tokens
            or profile.max_output_tokens
        )

        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        if request.temperature is not None:
            payload["temperature"] = request.temperature

        if request.top_p is not None:
            payload["top_p"] = request.top_p

        if request.stop:
            payload["stop"] = list(request.stop)

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                    },
                }
                for tool in request.tools
            ]

            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice

        extra_body = dict(profile.default_extra_body)

        if extra_body:
            payload["extra_body"] = extra_body

        if stream:
            payload["stream_options"] = {
                "include_usage": True,
            }

        return payload
```

---

## 11. DeepSeek Adapter

```python
class DeepSeekAdapter(OpenAICompatibleAdapter):
    name = "deepseek"

    def build_request(
        self,
        profile: ModelProfile,
        request: ChatRequest,
        *,
        stream: bool,
    ) -> dict:
        payload = super().build_request(
            profile,
            request,
            stream=stream,
        )

        extra_body = dict(
            payload.get("extra_body", {})
        )

        if request.disable_thinking:
            extra_body["thinking"] = {
                "type": "disabled",
            }
        elif profile.capabilities.thinking:
            extra_body.setdefault(
                "thinking",
                {"type": "enabled"},
            )

        if extra_body:
            payload["extra_body"] = extra_body

        return payload
```

响应解析时从 `reasoning_content` 提取 Thinking。

---

## 12. DashScope Adapter

```python
class DashScopeAdapter(OpenAICompatibleAdapter):
    name = "dashscope"

    def build_request(
        self,
        profile: ModelProfile,
        request: ChatRequest,
        *,
        stream: bool,
    ) -> dict:
        payload = super().build_request(
            profile,
            request,
            stream=stream,
        )

        extra_body = dict(
            payload.get("extra_body", {})
        )

        if request.disable_thinking:
            extra_body["enable_thinking"] = False
        elif profile.capabilities.thinking:
            extra_body.setdefault(
                "enable_thinking",
                True,
            )

        if extra_body:
            payload["extra_body"] = extra_body

        return payload
```

---

## 13. 统一错误模型

```python
# cogito/llm/errors.py

from dataclasses import dataclass


@dataclass
class LLMError(Exception):
    code: str
    message: str

    retryable: bool = False
    retry_after: float | None = None

    provider: str | None = None
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


class LLMAuthenticationError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMConnectionError(LLMError):
    pass


class ContentSafetyError(LLMError):
    pass


class ContextLengthError(LLMError):
    pass


class ModelCapabilityError(LLMError):
    pass


class InvalidLLMResponseError(LLMError):
    pass
```

重试规则：

| 错误 | 是否重试 |
|---|---|
| 网络连接临时失败 | 是 |
| 请求超时 | 是，次数有限 |
| HTTP 429 | 是，优先使用 Retry-After |
| HTTP 5xx | 是 |
| API Key 无效 | 否 |
| 请求参数错误 | 否 |
| 内容安全拒绝 | 否 |
| 上下文超长 | Provider 不重试，由上层缩减上下文 |
| 模型能力不支持 | 否 |
| 流已经输出内容后中断 | 默认不整段重试 |

---

## 14. ChatBackend

```python
# cogito/llm/backend.py

import asyncio
import random

from openai import AsyncOpenAI


class ChatBackend(ChatProvider):
    def __init__(
        self,
        *,
        provider_name: str,
        client: AsyncOpenAI,
        adapter: ProviderAdapter,
        profile: ModelProfile,
        request_timeout_s: float = 180.0,
        stream_idle_timeout_s: float = 90.0,
        max_retries: int = 2,
        retry_base_delay_s: float = 1.0,
        retry_max_delay_s: float = 30.0,
    ):
        self._provider_name = provider_name
        self._client = client
        self._adapter = adapter
        self._profile = profile

        self._request_timeout_s = request_timeout_s
        self._stream_idle_timeout_s = stream_idle_timeout_s

        self._max_retries = max_retries
        self._retry_base_delay_s = retry_base_delay_s
        self._retry_max_delay_s = retry_max_delay_s

    async def complete(
        self,
        request: ChatRequest,
    ) -> LLMResponse:
        payload = self._adapter.build_request(
            self._profile,
            request,
            stream=False,
        )

        raw = await self._request_with_retry(payload)

        return self._adapter.parse_response(
            raw,
            self._profile,
        )

    async def _request_with_retry(
        self,
        payload: dict,
    ):
        for attempt in range(self._max_retries + 1):
            try:
                async with asyncio.timeout(
                    self._request_timeout_s
                ):
                    return await (
                        self._client
                        .chat
                        .completions
                        .create(**payload)
                    )

            except asyncio.CancelledError:
                raise

            except TimeoutError as exc:
                error = LLMTimeoutError(
                    code="request_timeout",
                    message="LLM request timed out",
                    retryable=True,
                    provider=self._provider_name,
                )

            except Exception as exc:
                error = self._adapter.map_error(exc)

            if not error.retryable:
                raise error from exc

            if attempt >= self._max_retries:
                raise error from exc

            delay = (
                error.retry_after
                if error.retry_after is not None
                else self._retry_delay(attempt)
            )

            await asyncio.sleep(delay)

        raise RuntimeError("unreachable")

    def _retry_delay(self, attempt: int) -> float:
        raw = min(
            self._retry_base_delay_s * (2**attempt),
            self._retry_max_delay_s,
        )

        jitter = raw * 0.2

        return max(
            0.0,
            raw + random.uniform(-jitter, jitter),
        )

    async def close(self) -> None:
        await self._client.close()
```

创建 SDK Client 时关闭 SDK 自带重试：

```python
client = AsyncOpenAI(
    api_key=api_key,
    base_url=base_url,
    max_retries=0,
)
```

---

## 15. 流式重试原则

1. 打开 Stream 前失败，可以重试。
2. 首个有效 Delta 前失败，可以重试。
3. 已经输出 Delta 后断流，不自动从头重试。
4. 每个 Chunk 使用 Idle Timeout。
5. `CancelledError` 必须继续向上传播。
6. Tool Call Delta 必须完整保留。

---

## 16. ModelRegistry

```python
# cogito/llm/registry.py

class UnknownModelError(KeyError):
    pass


class ModelRegistry:
    def __init__(
        self,
        models: dict[str, ChatProvider],
    ):
        self._models = dict(models)

    def get(self, name: str) -> ChatProvider:
        try:
            return self._models[name]
        except KeyError as exc:
            raise UnknownModelError(
                f"unknown model profile: {name}"
            ) from exc

    async def close(self) -> None:
        seen: set[int] = set()

        for provider in self._models.values():
            identity = id(provider)

            if identity in seen:
                continue

            seen.add(identity)
            await provider.close()
```

---

## 17. LLMService

```python
# cogito/llm/service.py

class UnknownModelRoleError(KeyError):
    pass


class LLMService:
    def __init__(
        self,
        *,
        registry: ModelRegistry,
        routes: dict[str, str],
    ):
        self._registry = registry
        self._routes = dict(routes)

    def provider_for(
        self,
        role: str,
    ) -> ChatProvider:
        try:
            model_name = self._routes[role]
        except KeyError as exc:
            raise UnknownModelRoleError(
                f"unknown LLM role: {role}"
            ) from exc

        return self._registry.get(model_name)

    async def complete(
        self,
        role: str,
        request: ChatRequest,
    ) -> LLMResponse:
        provider = self.provider_for(role)
        return await provider.complete(request)

    def stream(
        self,
        role: str,
        request: ChatRequest,
    ):
        provider = self.provider_for(role)
        return provider.stream(request)

    async def close(self) -> None:
        await self._registry.close()
```

调用：

```python
response = await llm.complete(
    role="main",
    request=request,
)
```

---

## 18. Embedding

Embedding 不依赖 ChatBackend。

```python
# cogito/llm/embedding.py

from dataclasses import dataclass
from typing import Protocol

import httpx


class Embedder(Protocol):
    async def embed(
        self,
        text: str,
    ) -> list[float]:
        ...

    async def embed_batch(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        ...

    async def close(self) -> None:
        ...


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: str
    model: str
    base_url: str
    dimensions: int | None = None
    max_batch_size: int = 10
```

Embedding 批处理不应固定 `sleep(0.3)`。正常情况下直接处理下一批，收到 429 后按 Retry-After 退避。

---

## 19. 配置优先级

```text
代码默认值
  < config.toml
  < 环境变量覆盖
  < CLI 参数
```

用途：

| 来源 | 用途 |
|---|---|
| 代码默认值 | 安全默认配置 |
| TOML | 项目长期配置 |
| 环境变量 | Secret、部署环境覆盖 |
| CLI | 临时指定配置文件路径 |

---

## 20. 最终 config.toml

```toml
[app]
name = "cogito"
environment = "development"

[agent]
system_prompt_file = "prompts/system.md"
show_thinking = false

[loop]
inbound_queue_size = 100
session_mailbox_size = 20
max_concurrent_sessions = 4
turn_timeout_s = 300.0
provider_timeout_s = 180.0
tool_timeout_s = 120.0

[storage]
sqlite_path = "data/cogito.db"

[delivery]
channel_queue_size = 100
send_timeout_s = 30.0
retry_max_attempts = 5
retry_base_delay_s = 2.0
retry_max_delay_s = 300.0

# Provider 端点

[llm.providers.deepseek]
adapter = "deepseek"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"

request_timeout_s = 180.0
stream_idle_timeout_s = 90.0
max_retries = 2
retry_base_delay_s = 1.0
retry_max_delay_s = 30.0

[llm.providers.deepseek.default_headers]

[llm.providers.dashscope]
adapter = "dashscope"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"

request_timeout_s = 180.0
stream_idle_timeout_s = 90.0
max_retries = 2
retry_base_delay_s = 1.0
retry_max_delay_s = 30.0

[llm.providers.dashscope.default_headers]

# 模型

[llm.models.main]
provider = "deepseek"
model = "deepseek-chat"
max_output_tokens = 8192
capabilities = [
  "text",
  "tools",
  "thinking",
  "streaming",
]

[llm.models.main.extra_body]
thinking = { type = "enabled" }

[llm.models.light]
provider = "dashscope"
model = "qwen-plus"
max_output_tokens = 2048
capabilities = [
  "text",
  "tools",
  "streaming",
]

[llm.models.light.extra_body]
enable_thinking = false

[llm.models.vision]
provider = "dashscope"
model = "qwen-vl-plus"
max_output_tokens = 4096
capabilities = [
  "text",
  "vision",
  "streaming",
]

[llm.models.vision.extra_body]
enable_thinking = false

[llm.models.embedding]
provider = "dashscope"
model = "text-embedding-v3"
capabilities = [
  "embedding",
]

dimensions = 1024
max_batch_size = 10

# 逻辑路由

[llm.routes]
main = "main"
memory_gate = "light"
summary = "light"
vision = "vision"
embedding = "embedding"
```

环境变量：

```bash
export DEEPSEEK_API_KEY="sk-..."
export DASHSCOPE_API_KEY="sk-..."
```

不要把 API Key 写入 TOML。

---

## 21. 配置 Schema

```python
# cogito/config/schema.py

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class AppMetaConfig(BaseModel):
    name: str = "cogito"
    environment: str = "development"


class AgentConfig(BaseModel):
    system_prompt_file: Path = Path(
        "prompts/system.md"
    )
    show_thinking: bool = False


class LoopConfig(BaseModel):
    inbound_queue_size: int = 100
    session_mailbox_size: int = 20
    max_concurrent_sessions: int = 4

    turn_timeout_s: float = 300.0
    provider_timeout_s: float = 180.0
    tool_timeout_s: float = 120.0


class StorageConfig(BaseModel):
    sqlite_path: Path = Path("data/cogito.db")


class DeliveryConfig(BaseModel):
    channel_queue_size: int = 100
    send_timeout_s: float = 30.0

    retry_max_attempts: int = 5
    retry_base_delay_s: float = 2.0
    retry_max_delay_s: float = 300.0


class ProviderConfig(BaseModel):
    adapter: Literal[
        "openai",
        "openai_compatible",
        "deepseek",
        "dashscope",
    ]

    base_url: str
    api_key_env: str

    request_timeout_s: float = 180.0
    stream_idle_timeout_s: float = 90.0

    max_retries: int = 2
    retry_base_delay_s: float = 1.0
    retry_max_delay_s: float = 30.0

    default_headers: dict[str, str] = Field(
        default_factory=dict
    )


class ModelConfig(BaseModel):
    provider: str
    model: str

    max_output_tokens: int = 4096

    capabilities: set[str] = Field(
        default_factory=lambda: {
            "text",
            "streaming",
        }
    )

    extra_body: dict[str, Any] = Field(
        default_factory=dict
    )

    dimensions: int | None = None
    max_batch_size: int = 10


class LLMConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    routes: dict[str, str]


class AppConfig(BaseModel):
    app: AppMetaConfig = Field(
        default_factory=AppMetaConfig
    )
    agent: AgentConfig = Field(
        default_factory=AgentConfig
    )
    loop: LoopConfig = Field(
        default_factory=LoopConfig
    )
    storage: StorageConfig = Field(
        default_factory=StorageConfig
    )
    delivery: DeliveryConfig = Field(
        default_factory=DeliveryConfig
    )

    llm: LLMConfig

    config_path: Path | None = None
    project_dir: Path | None = None

    def resolve_path(self, value: Path) -> Path:
        if value.is_absolute():
            return value.resolve()

        base = self.project_dir or Path.cwd()
        return (base / value).resolve()
```

---

## 22. 配置加载器

```python
# cogito/config/loader.py

from __future__ import annotations

import json
import os
import re
import tomllib

from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .schema import AppConfig


class ConfigError(RuntimeError):
    pass


_ENV_PATTERN = re.compile(
    r"\$\{([A-Z_][A-Z0-9_]*)\}"
)


def find_config_path(
    explicit_path: str | Path | None,
) -> Path:
    if explicit_path is not None:
        path = Path(
            explicit_path
        ).expanduser().resolve()

        if not path.is_file():
            raise ConfigError(
                f"config file not found: {path}"
            )

        return path

    env_path = os.getenv("COGITO_CONFIG")

    if env_path:
        path = Path(
            env_path
        ).expanduser().resolve()

        if not path.is_file():
            raise ConfigError(
                "COGITO_CONFIG points to a "
                f"missing file: {path}"
            )

        return path

    candidates = (
        Path.cwd() / "config.toml",
        Path.cwd() / "cogito.toml",
        Path.home()
        / ".config"
        / "cogito"
        / "config.toml",
    )

    for path in candidates:
        if path.is_file():
            return path.resolve()

    raise ConfigError(
        "configuration file not found; "
        "use --config, COGITO_CONFIG, "
        "or create config.toml"
    )


def expand_env_in_value(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.getenv(name)

            if resolved is None:
                raise ConfigError(
                    "required environment "
                    f"variable is not set: {name}"
                )

            return resolved

        return _ENV_PATTERN.sub(replace, value)

    if isinstance(value, list):
        return [
            expand_env_in_value(item)
            for item in value
        ]

    if isinstance(value, dict):
        return {
            key: expand_env_in_value(item)
            for key, item in value.items()
        }

    return value


def parse_env_value(raw: str) -> Any:
    lowered = raw.lower()

    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def set_nested(
    target: dict[str, Any],
    path: list[str],
    value: Any,
) -> None:
    current = target

    for part in path[:-1]:
        child = current.get(part)

        if not isinstance(child, dict):
            child = {}
            current[part] = child

        current = child

    current[path[-1]] = value


def apply_environment_overrides(
    data: dict[str, Any],
    *,
    prefix: str = "COGITO__",
) -> dict[str, Any]:
    result = deepcopy(data)

    for name, raw_value in os.environ.items():
        if not name.startswith(prefix):
            continue

        path = [
            part.lower()
            for part in (
                name[len(prefix):].split("__")
            )
            if part
        ]

        if not path:
            continue

        set_nested(
            result,
            path,
            parse_env_value(raw_value),
        )

    return result


def load_config(
    path: str | Path | None = None,
) -> AppConfig:
    config_path = find_config_path(path)

    try:
        with config_path.open("rb") as file:
            raw = tomllib.load(file)

    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"invalid TOML in {config_path}: {exc}"
        ) from exc

    raw = expand_env_in_value(raw)
    raw = apply_environment_overrides(raw)

    raw["config_path"] = config_path
    raw["project_dir"] = config_path.parent

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"invalid configuration: {exc}"
        ) from exc

    validate_config_references(config)
    return config


def validate_config_references(
    config: AppConfig,
) -> None:
    for model_name, model in config.llm.models.items():
        if model.provider not in config.llm.providers:
            raise ConfigError(
                f"model {model_name!r} references "
                f"unknown provider {model.provider!r}"
            )

    for role, model_name in config.llm.routes.items():
        if model_name not in config.llm.models:
            raise ConfigError(
                f"route {role!r} references "
                f"unknown model {model_name!r}"
            )
```

环境变量覆盖示例：

```bash
export COGITO__LOOP__MAX_CONCURRENT_SESSIONS=2
export COGITO__LLM__MODELS__MAIN__MAX_OUTPUT_TOKENS=4096
```

---

## 23. API Key 解析

```python
# cogito/bootstrap/providers.py

import os


def resolve_api_key(
    provider_name: str,
    config: ProviderConfig,
) -> str:
    value = os.getenv(config.api_key_env)

    if not value:
        raise ConfigError(
            f"missing API key for provider "
            f"{provider_name!r}; set "
            f"{config.api_key_env}"
        )

    return value
```

日志中只记录环境变量名，不记录 Secret。

---

## 24. Bootstrap Provider

```python
# cogito/bootstrap/providers.py

from openai import AsyncOpenAI


def build_capabilities(
    values: set[str],
) -> ModelCapabilities:
    return ModelCapabilities(
        text="text" in values,
        tools="tools" in values,
        vision="vision" in values,
        thinking="thinking" in values,
        streaming="streaming" in values,
        embedding="embedding" in values,
    )


def build_llm_service(
    config: AppConfig,
) -> LLMService:
    models: dict[str, ChatBackend] = {}

    for model_name, model_config in (
        config.llm.models.items()
    ):
        if "embedding" in model_config.capabilities:
            continue

        provider_config = (
            config.llm.providers[
                model_config.provider
            ]
        )

        try:
            adapter_factory = (
                ADAPTER_FACTORIES[
                    provider_config.adapter
                ]
            )
        except KeyError as exc:
            raise ConfigError(
                "unknown provider adapter: "
                f"{provider_config.adapter}"
            ) from exc

        api_key = resolve_api_key(
            model_config.provider,
            provider_config,
        )

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=provider_config.base_url,
            default_headers=(
                provider_config.default_headers
            ),
            max_retries=0,
        )

        profile = ModelProfile(
            name=model_name,
            provider=model_config.provider,
            model=model_config.model,
            capabilities=build_capabilities(
                model_config.capabilities
            ),
            max_output_tokens=(
                model_config.max_output_tokens
            ),
            default_extra_body=dict(
                model_config.extra_body
            ),
        )

        models[model_name] = ChatBackend(
            provider_name=model_config.provider,
            client=client,
            adapter=adapter_factory(),
            profile=profile,
            request_timeout_s=(
                provider_config.request_timeout_s
            ),
            stream_idle_timeout_s=(
                provider_config.stream_idle_timeout_s
            ),
            max_retries=provider_config.max_retries,
            retry_base_delay_s=(
                provider_config.retry_base_delay_s
            ),
            retry_max_delay_s=(
                provider_config.retry_max_delay_s
            ),
        )

    registry = ModelRegistry(models)

    return LLMService(
        registry=registry,
        routes=config.llm.routes,
    )
```

---

## 25. Prompt 读取

System Prompt 应由 Bootstrap 或 PromptRegistry 读取，而不是存进 Provider。

```python
def load_system_prompt(
    config: AppConfig,
) -> str:
    path = config.resolve_path(
        config.agent.system_prompt_file
    )

    if not path.is_file():
        raise ConfigError(
            f"system prompt file not found: {path}"
        )

    return path.read_text(
        encoding="utf-8"
    ).strip()
```

所有相对路径都以配置文件目录为基准。

---

## 26. Application Bootstrap

```python
# cogito/bootstrap/application.py

async def create_application(
    config_path: str | None = None,
) -> Application:
    config = load_config(config_path)

    system_prompt = load_system_prompt(config)
    llm_service = build_llm_service(config)
    embedder = build_embedder(config)

    session_store = SessionStore(
        path=config.resolve_path(
            config.storage.sqlite_path
        )
    )
    await session_store.open()

    prompt_builder = PromptBuilder(
        system_prompt=system_prompt
    )

    turn_runner = TurnRunner(
        llm=llm_service,
        embedder=embedder,
        prompts=prompt_builder,
        # sessions / tools / hooks / events ...
    )

    return Application(
        config=config,
        llm=llm_service,
        embedder=embedder,
        session_store=session_store,
        turn_runner=turn_runner,
    )
```

---

## 27. AgentLoopDeps

```python
@dataclass(frozen=True)
class AgentLoopDeps:
    llm: LLMService
    embedder: Embedder | None

    sessions: SessionManager
    tools: ToolRegistry

    events: DomainEventBus
    hooks: HookPipeline
```

不要分别注入：

```python
main_provider
light_provider
vision_provider
```

模型映射由 `LLMService` 管理。

---

## 28. TurnRunner 使用方式

普通对话：

```python
response = await deps.llm.complete(
    role="main",
    request=request,
)
```

记忆门控：

```python
response = await deps.llm.complete(
    role="memory_gate",
    request=gate_request,
)
```

图片请求：

```python
role = (
    "vision"
    if request_contains_image(request)
    else "main"
)

response = await deps.llm.complete(
    role=role,
    request=request,
)
```

---

## 29. 流式输出与消息系统

```text
Provider
  → LLMStreamEvent
  → TurnRunner
  → StreamSink
  → Channel 临时展示
```

最终完整消息仍然走：

```text
TurnCoordinator
  → SQLite Transaction
  → Outbox
  → DeliveryManager
  → Channel.send()
```

流式 Delta 不逐 Token 写入 Outbox。

Telegram 等平台应通过编辑同一条临时消息实现流式展示，而不是为每个 Delta 新发消息。

---

## 30. CLI 入口

```python
# cogito/__main__.py

import argparse
import asyncio


def parse_args():
    parser = argparse.ArgumentParser(
        prog="cogito"
    )

    parser.add_argument(
        "--config",
        help="path to config.toml",
    )

    return parser.parse_args()


async def async_main(
    config_path: str | None,
) -> None:
    app = await create_application(config_path)

    try:
        await app.run()
    finally:
        await app.close()


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args.config))


if __name__ == "__main__":
    main()
```

启动：

```bash
python -m cogito --config ./config.toml
```

或者：

```bash
export COGITO_CONFIG=/path/to/config.toml
python -m cogito
```

---

## 31. 配置热更新

Cogito v1 不建议运行中热更新 Provider。

推荐流程：

```text
修改 config.toml
  → 重启 Cogito
```

未来如果需要热更新，应先完整构建和验证新的 `LLMService`，再原子替换，并等待旧请求结束后关闭旧 Client。

---

## 32. 核心不变量

1. Provider 不读取配置文件。
2. Provider 不读取环境变量。
3. Provider 不保存 Agent System Prompt。
4. Provider 不引用 Channel、Session 或 MessageBus。
5. 业务层不直接使用厂商模型名。
6. Adapter 不通过 Base URL 猜测厂商。
7. API Key 不写入 TOML。
8. API Key 不进入日志。
9. 不支持的能力必须明确报错或路由。
10. 不静默删除图片。
11. Tool Call 必须保留原始参数。
12. `CancelledError` 必须传播。
13. SDK 和项目只保留一层重试。
14. 流式输出开始后不自动整段重试。
15. Embedding 与 Chat Provider 独立。
16. 相对路径以配置文件目录为基准。
17. Provider Client 必须支持关闭。
18. 最终 Assistant 消息仍由 TurnCoordinator 和 Outbox 提交。

---

## 33. 最终数据流

```text
config.toml
+ environment
+ CLI --config
       │
       ▼
load_config()
       │
       ▼
AppConfig
       │
       ▼
Bootstrap
       ├── PromptBuilder
       ├── LLMService
       ├── Embedder
       ├── SessionStore
       └── TurnRunner
               │
               ▼
          LLMService
               │
               ▼
         ModelRegistry
               │
               ▼
          ChatBackend
               │
               ▼
        ProviderAdapter
               │
               ▼
          外部模型 API
```

这套结构把职责分为三层：

```text
配置层：项目选择什么模型
业务层：为什么、何时调用模型
Provider 层：如何调用外部模型
```

因此可以做到：

- 换模型不改业务代码；
- 新增厂商只新增 Adapter；
- Provider 不被项目配置污染；
- 流式输出不侵入 Channel；
- Secret 不落盘；
- 模型能力显式校验；
- Chat 与 Embedding 清晰分离；
- 后续接入 Phase、Memory 和 Proactive 时无需重新设计 Provider 边界。
