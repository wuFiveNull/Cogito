# Cogito-Agent Tool System 最终设计与实现规格

> 本文档定义 Cogito-Agent 的完整 Tool 子系统，包括内建 Tool、动态 Tool Discovery、Tool Search、审批策略、执行中间件、结果治理、沙箱、MCP、可观测性、持久化、恢复机制与测试标准。
>
> 本设计直接融入现有 `RuntimeKernel + Ordered Phases + Ports` 架构，不引入第二套 Agent Runtime，不让 Kernel 感知具体 Tool、MCP、Channel 或 MessageBus 实现。

---

## 1. 文档目标

本文档用于直接指导生产级实现。完成后，Cogito-Agent 应具备以下能力：

1. 用统一强类型协议定义内建 Tool、第三方 Tool 和 MCP Tool。
2. 通过显式注册、快照化 Registry 和 Tool Catalog 管理工具。
3. 根据用户、会话、策略、模型能力和当前任务动态决定可见工具。
4. 在 `AgentLoopPhase` 内完成 Model → Tool → Model 循环。
5. 对所有 Tool 调用执行参数校验、权限判断、审批、限流、并发控制、超时、结果裁剪、持久化和审计。
6. 对文件系统、Shell、网络和 MCP 请求实施统一安全边界。
7. 支持同步工具、异步工具、流式工具、可取消工具、独占工具和可并行只读工具。
8. 支持超大结果落盘、Artifact 引用、多模态内容、上下文预算和历史压缩。
9. 支持 MCP 的 stdio、SSE、Streamable HTTP、重连、热刷新和原子替换。
10. 保持 Channel、MessageBus、Runtime Kernel、Tool Infrastructure 之间的依赖方向清晰可测试。

---

## 2. 核心架构决策

### 2.1 Tool 不增加新的顶层 Phase

Tool 能力分布在两个既有 Phase 中：

- `ContextAssemblyPhase`
  - 查询 Tool Catalog。
  - 计算当前可见 Tool 集合。
  - 将 Tool Schema 注入模型请求。
- `AgentLoopPhase`
  - 解析模型 Tool Call。
  - 调用 Tool Orchestrator。
  - 将 Tool Result 注入模型上下文。
  - 执行下一轮模型调用。

审批、安全、沙箱、MCP、限流、结果落盘等均属于 Tool 子系统内部职责，不应进入 `RuntimeKernel` 主循环。

### 2.2 Definition、Selection、Execution、Infrastructure 分离

```text
Tool Definition
    定义名称、Schema、风险、能力、来源
        ↓
Tool Registry
    保存不可变 Tool 快照，处理冲突与版本
        ↓
Tool Catalog / Selector
    根据 actor、session、policy、query 选择可见工具
        ↓
Tool Orchestrator
    执行完整调用流水线
        ↓
Tool Handler / MCP Adapter / Sandbox Adapter
    完成真实副作用
```

禁止把 Schema、权限判断、执行逻辑、MCP 连接和 UI 展示全部放进一个 Tool 类。

### 2.3 Registry 采用显式组装，不采用隐式模块扫描

内建工具、插件 Provider、MCP Provider 均由 Composition Root 显式注入。

允许：

- `BuiltinToolProvider`
- `EntryPointToolProvider`
- `McpToolProvider`
- `ConfiguredToolProvider`

不允许：

- Runtime import 时扫描全部 Python 文件。
- 模块 import 时自动连接外部服务。
- Tool 自己写入全局单例 Registry。
- Tool 覆盖同名 Tool 而无明确冲突策略。

### 2.4 Tool Result 必须区分模型内容、用户展示和完整原始结果

一个执行结果至少存在三种视图：

1. `llm_content`：回馈模型，受 Token/字符预算控制。
2. `display_content`：面向用户或 TUI，不等同于原始 stdout。
3. `artifact_refs`：完整结果、文件、图片、日志等持久化引用。

任何大结果都不得直接无上限进入模型上下文。

### 2.5 所有外部内容默认不可信

Web、Shell、MCP、文件内容和第三方 API 返回值均视为不可信数据：

- 不能把 Tool 输出解释为系统指令。
- 不能允许 Tool 输出修改系统 Prompt 或权限策略。
- MCP 描述和结果应做长度限制、控制字符清理和 Prompt Injection 标记。
- 密钥、Cookie、Token、环境变量必须在日志和 Tool Result 中脱敏。

---

## 3. 总体架构

```text
┌─────────────────────────────────────────────────────────────┐
│ Channel / MessageBus / AgentMessageWorker                  │
└────────────────────────────┬────────────────────────────────┘
                             │ AgentRequest
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ RuntimeKernel                                               │
│ Ordered Phases                                              │
│                                                             │
│ TurnInit → StateLoad → Retrieval → ContextAssembly          │
│                                      │                      │
│                                      ├─ ToolCatalog.select  │
│                                      └─ visible schemas     │
│                                                             │
│ AgentLoop                                                   │
│   ModelGateway                                              │
│      ↓ tool_calls                                           │
│   ToolOrchestrator                                          │
│      ├─ Registry / Resolver                                 │
│      ├─ Schema Validator                                    │
│      ├─ Policy Engine                                       │
│      ├─ Approval Coordinator                                │
│      ├─ Rate / Concurrency / Timeout                        │
│      ├─ Tool Handler / MCP Adapter                          │
│      ├─ Result Processor / Artifact Store                   │
│      └─ Audit / Events                                      │
│                                                             │
│ KnowledgeExtraction → Persistence → TurnFinalize            │
└────────────────────────────┬────────────────────────────────┘
                             │ Ports
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ Infrastructure                                              │
│ File / Shell / Web / MCP / DB / Sandbox / Secret Store     │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 推荐目录结构

```text
cogito_agent/
├── application/
│   ├── agent_service.py
│   ├── approvals/
│   │   ├── service.py
│   │   ├── models.py
│   │   └── ports.py
│   └── messaging/
│       ├── envelope.py
│       ├── mapper.py
│       ├── ports.py
│       └── worker.py
│
├── runtime/
│   ├── kernel.py
│   ├── context.py
│   ├── events.py
│   ├── errors.py
│   ├── models.py
│   ├── cleanup.py
│   ├── phase.py
│   └── phases/
│       ├── context_assembly.py
│       └── agent_loop.py
│
├── domain/
│   ├── messages.py
│   ├── usage.py
│   └── tools/
│       ├── __init__.py
│       ├── definition.py
│       ├── invocation.py
│       ├── result.py
│       ├── policy.py
│       ├── approval.py
│       ├── artifacts.py
│       ├── errors.py
│       └── records.py
│
├── ports/
│   ├── model.py
│   ├── events.py
│   ├── tools/
│   │   ├── catalog.py
│   │   ├── registry.py
│   │   ├── executor.py
│   │   ├── policy.py
│   │   ├── approval.py
│   │   ├── artifacts.py
│   │   ├── audit.py
│   │   ├── checkpoint.py
│   │   ├── rate_limit.py
│   │   └── sandbox.py
│   └── mcp/
│       ├── manager.py
│       ├── client.py
│       └── config.py
│
├── tools/
│   ├── registry.py
│   ├── catalog.py
│   ├── selector.py
│   ├── orchestrator.py
│   ├── middleware.py
│   ├── validation.py
│   ├── coercion.py
│   ├── result_processor.py
│   ├── context_governor.py
│   ├── repetition_guard.py
│   ├── concurrency.py
│   ├── providers.py
│   └── builtin/
│       ├── tool_search.py
│       ├── filesystem.py
│       ├── shell.py
│       ├── web.py
│       ├── memory.py
│       ├── messaging.py
│       └── time.py
│
├── infrastructure/
│   ├── tools/
│   │   ├── artifact_store.py
│   │   ├── audit_repository.py
│   │   ├── approval_repository.py
│   │   ├── checkpoint_repository.py
│   │   └── secret_redactor.py
│   ├── sandbox/
│   │   ├── command_policy.py
│   │   ├── process_runner.py
│   │   ├── workspace_scope.py
│   │   ├── network_policy.py
│   │   ├── linux_bwrap.py
│   │   ├── macos_seatbelt.py
│   │   └── windows_job.py
│   └── mcp/
│       ├── client.py
│       ├── manager.py
│       ├── adapter.py
│       ├── transport.py
│       ├── oauth.py
│       ├── schema_converter.py
│       └── content_converter.py
│
├── bootstrap/
│   ├── runtime_factory.py
│   ├── tool_factory.py
│   └── settings.py
│
└── tests/
    ├── unit/tools/
    ├── integration/tools/
    ├── integration/mcp/
    ├── security/
    └── architecture/
```

`tools/` 是 Tool 应用编排层；`infrastructure/tools/` 是具体存储与安全实现；`domain/tools/` 不导入任何基础设施模块。

---

## 5. Tool 领域模型

### 5.1 枚举

```python
from __future__ import annotations

from enum import StrEnum


class ToolKind(StrEnum):
    READ = "read"
    SEARCH = "search"
    FETCH = "fetch"
    EDIT = "edit"
    EXECUTE = "execute"
    COMMUNICATE = "communicate"
    MEMORY = "memory"
    AGENT = "agent"
    ADMIN = "admin"


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"
    PRIVILEGED = "privileged"


class ToolSourceType(StrEnum):
    BUILTIN = "builtin"
    PLUGIN = "plugin"
    MCP = "mcp"
    REMOTE = "remote"


class ToolConcurrencyMode(StrEnum):
    PARALLEL_SAFE = "parallel_safe"
    SERIAL_PER_SESSION = "serial_per_session"
    SERIAL_PER_TOOL = "serial_per_tool"
    EXCLUSIVE = "exclusive"


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
```

风险等级采用五级而非简单三分法：外部读取与本地只读具有不同的数据泄漏风险；外部写入与本地写入具有不同的副作用范围。

### 5.2 ToolDefinition

```python
from dataclasses import dataclass, field
from typing import Mapping

JsonSchema = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolSource:
    type: ToolSourceType
    provider: str
    version: str | None = None
    server_name: str | None = None


@dataclass(frozen=True, slots=True)
class ToolLimits:
    timeout_seconds: float = 60.0
    max_result_chars: int = 50_000
    max_result_bytes: int = 2_000_000
    max_concurrency: int = 4
    rate_limit_key: str | None = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: JsonSchema
    kind: ToolKind
    risk: ToolRisk
    source: ToolSource

    output_schema: JsonSchema | None = None
    tags: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset({"core"})
    required_capabilities: frozenset[str] = frozenset()

    always_visible: bool = False
    idempotent: bool = False
    deterministic: bool = False
    concurrency_mode: ToolConcurrencyMode = ToolConcurrencyMode.SERIAL_PER_SESSION
    limits: ToolLimits = field(default_factory=ToolLimits)

    enabled: bool = True
    deprecated: bool = False
    replacement: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
```

约束：

- `name` 必须满足 `^[a-z][a-z0-9_]{0,63}$`。
- 内建 Tool 不使用 `mcp_` 前缀。
- MCP Tool 规范名为 `mcp_{server_slug}_{tool_slug}`。
- `input_schema` 必须是 JSON Schema object，根节点 `type` 必须为 `object`。
- Tool 描述最大 2,000 字符；单参数描述最大 1,000 字符。
- `metadata` 不能保存 Handler、连接对象或 Secret。

### 5.3 ToolCall 与 ToolExecutionContext

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, object]
    model_call_id: str | None = None
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    turn_id: str
    request_id: str
    session_id: str
    actor_id: str
    trace_id: str | None
    workspace_id: str | None
    workspace_root: str | None
    locale: str | None
    timezone: str | None
    started_at: datetime
    cancellation_token: object
    metadata: Mapping[str, object] = field(default_factory=dict)
```

`ToolExecutionContext.metadata` 只允许可序列化、非敏感、Channel 无关的数据。

### 5.4 Tool Content 与 Artifact

```python
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str


@dataclass(frozen=True, slots=True)
class JsonContent:
    value: object


@dataclass(frozen=True, slots=True)
class ImageContent:
    media_type: str
    artifact_id: str
    alt_text: str | None = None


ToolContent = TextContent | JsonContent | ImageContent


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    media_type: str
    size_bytes: int
    sha256: str
    storage_uri: str
    name: str | None = None
    expires_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### 5.5 ToolResult

```python
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ToolErrorInfo:
    code: str
    safe_message: str
    retryable: bool
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: ToolResultStatus
    llm_content: tuple[ToolContent, ...]
    display_content: tuple[ToolContent, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    error: ToolErrorInfo | None = None
    duration_ms: int | None = None
    truncated: bool = False
    persisted: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)
```

规则：

- `SUCCEEDED` 可以没有展示内容，但 `llm_content` 必须非空；空结果统一转换为完成提示。
- `FAILED`、`DENIED`、`TIMED_OUT` 必须携带稳定错误码和安全消息。
- `llm_content` 不包含本地绝对路径、Secret 或完整堆栈。
- 完整原始输出如需保留，必须进入 Artifact Store。

---

## 6. Tool Handler 与 Provider 协议

### 6.1 ToolHandler

```python
from collections.abc import AsyncIterator
from typing import Protocol


class ToolHandler(Protocol):
    @property
    def definition(self) -> ToolDefinition:
        ...

    async def execute(
        self,
        *,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
    ) -> ToolResult:
        ...
```

所有真实 Tool 以异步接口暴露。同步 SDK 必须在 Adapter 内用线程池桥接，不能阻塞事件循环。

### 6.2 可选流式协议

```python
@dataclass(frozen=True, slots=True)
class ToolProgress:
    call_id: str
    message: str
    progress: float | None = None
    data: Mapping[str, object] = field(default_factory=dict)


class StreamingToolHandler(ToolHandler, Protocol):
    async def stream_execute(
        self,
        *,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
    ) -> AsyncIterator[ToolProgress | ToolResult]:
        ...
```

Orchestrator 只认统一的最终 `ToolResult`，进度事件通过 EventSink 发出，不进入长期模型上下文。

### 6.3 ToolProvider

```python
class ToolProvider(Protocol):
    @property
    def name(self) -> str:
        ...

    async def load(self) -> list[ToolHandler]:
        ...

    async def close(self) -> None:
        ...
```

Provider 负责发现或构造 Handler，但不直接修改 Registry。Bootstrap 加载 Provider 后，将完整结果一次性交给 Registry 发布新快照。

---

## 7. Tool Registry

### 7.1 职责

Registry 只负责：

- 注册和注销 Tool Handler。
- 校验定义。
- 处理同名冲突。
- 提供不可变快照。
- 按名称解析 Handler。
- 暴露版本号以支持缓存失效。

Registry 不负责：

- 用户权限。
- Tool 搜索排序。
- 审批。
- 执行。
- MCP 重连。
- 结果持久化。

### 7.2 快照模型

```python
@dataclass(frozen=True, slots=True)
class ToolRegistrySnapshot:
    version: int
    definitions: Mapping[str, ToolDefinition]
    handlers: Mapping[str, ToolHandler]
    created_at: datetime
```

Registry 内部更新必须原子化：先构造新字典并完成全部校验，再一次性替换当前快照。模型请求和 Tool 执行绑定同一个 Registry 版本，避免 Schema 与执行 Handler 不一致。

### 7.3 冲突策略

```python
class ToolConflictPolicy(StrEnum):
    ERROR = "error"
    KEEP_EXISTING = "keep_existing"
    REPLACE = "replace"
    RENAME_SOURCE = "rename_source"
```

默认优先级：

```text
builtin > configured plugin > entry-point plugin > MCP
```

生产默认策略为 `ERROR`。只有 Bootstrap 明确配置时才允许替换。

### 7.4 Registry 接口

```python
class ToolRegistryPort(Protocol):
    def snapshot(self) -> ToolRegistrySnapshot:
        ...

    def resolve(
        self,
        name: str,
        *,
        version: int | None = None,
    ) -> ToolHandler:
        ...

    async def replace_provider_tools(
        self,
        *,
        provider_name: str,
        handlers: list[ToolHandler],
    ) -> ToolRegistrySnapshot:
        ...
```

---

## 8. Tool Catalog、Toolset 与动态可见性

### 8.1 Toolset

Toolset 是配置层的具名组合，不是 Runtime Phase：

```yaml
toolsets:
  safe:
    include:
      - read_file
      - list_dir
      - grep_search
      - web_search
      - web_fetch
    include_sets: []

  coding:
    include:
      - write_file
      - edit_file
      - apply_patch
      - shell
    include_sets:
      - safe

  personal_assistant:
    include_sets:
      - safe
    include:
      - recall_memory
      - memorize
      - send_message
```

Toolset 解析必须做循环检测、缺失引用检测和结果去重。

### 8.2 ToolSelectionRequest

```python
@dataclass(frozen=True, slots=True)
class ToolSelectionRequest:
    actor_id: str
    session_id: str
    query: str
    requested_toolsets: tuple[str, ...]
    model_id: str
    model_max_tools: int
    registry_version: int
    allowed_risks: frozenset[ToolRisk]
    scopes: frozenset[str] = frozenset({"core"})
```

### 8.3 VisibleToolSet

```python
@dataclass(frozen=True, slots=True)
class VisibleToolSet:
    registry_version: int
    definitions: tuple[ToolDefinition, ...]
    selected_names: frozenset[str]
    deferred_names: frozenset[str]
    selection_reason: Mapping[str, str]
```

### 8.4 选择算法

选择顺序固定为：

1. Registry 中 `enabled=True` 且未废弃的工具。
2. 应用 Toolset include/exclude。
3. 应用 actor/session 权限与租户策略。
4. 应用 scope、模型能力和风险限制。
5. 加入 `always_visible=True` 工具。
6. 加入本会话最近成功使用的 Tool LRU。
7. 对其余工具按 query 与 description/tags 做搜索排序。
8. 截断到模型允许的最大 Tool 数和 Schema Token 预算。
9. 将其余工具标记为 deferred。

推荐常驻工具：

- `tool_search`
- `read_file`
- `list_dir`
- `get_current_time`

其余工具按需展开，避免把全部 MCP Schema 注入每轮上下文。

### 8.5 Tool Search

`tool_search` 是只读元工具，返回匹配的 Tool 摘要，不直接执行目标 Tool。

参数：

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "minLength": 1},
    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    "kinds": {
      "type": "array",
      "items": {"type": "string"}
    }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

Tool Search 结果触发下一次模型调用时扩展 `VisibleToolSet`，扩展结果记录到会话 LRU，但不得永久改变全局 Registry。

---

## 9. ContextAssemblyPhase 集成

`TurnContext` 新增强类型字段：

```python
@dataclass(slots=True)
class TurnContext:
    # existing fields omitted
    registry_version: int | None = None
    visible_tools: VisibleToolSet | None = None
    tool_round: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    tool_result_chars_in_context: int = 0
    pending_approval: ToolApprovalTicket | None = None
    loop_checkpoint_id: str | None = None
```

`ContextAssemblyPhase` 的 Tool 部分：

```python
selection = await tool_catalog.select(
    ToolSelectionRequest(
        actor_id=ctx.request.actor_id,
        session_id=ctx.request.session_id,
        query=ctx.request.text,
        requested_toolsets=resolved_toolsets,
        model_id=model_config.model_id,
        model_max_tools=model_config.max_tools,
        registry_version=tool_registry.snapshot().version,
        allowed_risks=policy.allowed_visible_risks,
    )
)

ctx.registry_version = selection.registry_version
ctx.visible_tools = selection
ctx.available_tools = schema_adapter.to_model_tools(selection.definitions)
```

Schema Adapter 只做模型供应商格式转换，不改变领域定义。

---

## 10. AgentLoopPhase 集成

### 10.1 模型响应模型

```python
@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    response_id: str
    text: str | None
    tool_calls: tuple[ModelToolCall, ...]
    usage: UsageSummary
    finish_reason: str
```

### 10.2 Agent Loop

```text
调用模型
  ├─ 无 tool_calls → 保存 final_response，结束
  └─ 有 tool_calls
       ├─ 校验最大轮数
       ├─ 解析 ToolCall
       ├─ 检查 Tool 是否在本轮 VisibleToolSet
       ├─ RepetitionGuard 检查循环
       ├─ 按并发模式分组执行
       ├─ 将 ToolResult 转为 tool message
       ├─ 执行上下文治理
       └─ 再次调用模型
```

### 10.3 并行规则

只在以下条件全部满足时并行：

- 同一模型响应包含多个 Tool Call。
- 所有调用的 `concurrency_mode == PARALLEL_SAFE`。
- 所有调用 `risk` 不高于 `EXTERNAL_READ`。
- 没有调用依赖同批其他调用的结果。
- 没有 Tool 标记 `EXCLUSIVE`。

其他情况按模型返回顺序串行执行。

不得仅凭 `read_only=True` 推断无资源冲突；例如读取同一个限流 API 仍可能需要串行或限流。

### 10.4 循环保护

至少实现：

- 最大 Tool 轮数，默认 8。
- 单 Tool 连续失败警告阈值 2。
- 相同 Tool + 规范化参数连续失败阻断阈值 4。
- 同一 Tool 总失败终止阈值 8。
- 幂等 Tool 相同参数成功后重复调用可直接返回缓存结果。
- 非幂等 Tool 永不自动重放。

调用指纹：

```text
sha256(tool_name + canonical_json(arguments) + actor_id + session_id)
```

Secret 字段应先替换为固定占位符再生成指纹。

---

## 11. Tool Orchestrator 执行流水线

### 11.1 固定执行顺序

```text
1. Resolve
2. Visibility Check
3. Argument Parse
4. Type Coercion
5. JSON Schema Validation
6. Context Enrichment
7. Policy Evaluation
8. Approval Resolution
9. Rate Limit
10. Concurrency Lock
11. Timeout / Cancellation Scope
12. Handler Execution
13. Output Schema Validation
14. Secret Redaction
15. Result Normalization
16. Truncation / Persistence / Artifact Materialization
17. Audit Record
18. Event Emission
19. Return ToolResult
```

顺序不可随意交换。尤其：

- 审批必须发生在副作用执行前。
- Secret Redaction 必须发生在日志、事件和模型注入前。
- Result 持久化必须在上下文裁剪前完成。

### 11.2 Orchestrator 接口

```python
class ToolExecutorPort(Protocol):
    async def execute(
        self,
        *,
        call: ToolCall,
        context: ToolExecutionContext,
        visible_tools: VisibleToolSet,
    ) -> ToolResult:
        ...

    async def execute_many(
        self,
        *,
        calls: tuple[ToolCall, ...],
        context: ToolExecutionContext,
        visible_tools: VisibleToolSet,
    ) -> tuple[ToolResult, ...]:
        ...
```

### 11.3 中间件扩展点

可以实现显式 Middleware 列表，但不允许隐式全局 Hook：

```python
class ToolMiddleware(Protocol):
    async def before(
        self,
        request: ToolExecutionRequest,
    ) -> ToolExecutionRequest:
        ...

    async def after(
        self,
        request: ToolExecutionRequest,
        result: ToolResult,
    ) -> ToolResult:
        ...
```

Middleware 顺序由 Bootstrap 固定配置。安全 Middleware 必须 fail-closed；观测性 Middleware 可以 fail-open。

---

## 12. 参数解析、强制转换与 Schema 校验

### 12.1 JSON 解析

- 模型参数必须是单个 JSON object。
- 禁止 `NaN`、`Infinity`、重复 key 和 JSON 注释。
- 最大参数体默认 256 KB。
- 解析失败返回 `TOOL_ARGUMENT_JSON_INVALID`。

### 12.2 类型强制转换

仅允许无歧义转换：

- `"12"` → integer 12。
- `"true"` / `"false"` → boolean。
- 单字符串 → 单元素字符串数组，仅当 Schema 明确允许且配置开启。

禁止：

- 任意字符串执行 `eval`。
- 将对象静默转为字符串。
- 将无效日期猜测为合法日期。
- 丢弃未知参数。

### 12.3 JSON Schema

建议使用 Draft 2020-12 验证器，并在注册时编译缓存。

所有 Tool 根 Schema 默认补充：

```json
{
  "type": "object",
  "additionalProperties": false
}
```

注册时拒绝：

- 无界递归 `$ref`。
- 超大 enum。
- 远程 `$ref`。
- 不受支持的自定义格式。
- Schema 深度超过配置上限。

---

## 13. 权限策略系统

### 13.1 决策模型

```python
class PolicyDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    type: PolicyDecisionType
    reason_code: str
    safe_message: str
    approval_scope: str | None = None
    constraints: Mapping[str, object] = field(default_factory=dict)
```

### 13.2 ToolPolicyRequest

```python
@dataclass(frozen=True, slots=True)
class ToolPolicyRequest:
    definition: ToolDefinition
    arguments: Mapping[str, object]
    actor_id: str
    session_id: str
    workspace_id: str | None
    channel_capabilities: frozenset[str]
    prior_grants: tuple[ToolPermissionGrant, ...]
```

### 13.3 策略合并

按最严格原则合并：

```text
platform policy
  ∩ deployment policy
  ∩ workspace policy
  ∩ actor policy
  ∩ session grant
  ∩ tool-specific guard
```

任一层 `DENY` 则最终 `DENY`；无 `DENY` 且任一层要求审批则 `REQUIRE_APPROVAL`。

### 13.4 默认风险策略

| 风险 | 默认行为 |
|---|---|
| `READ_ONLY` | 允许，但仍做路径与数据边界检查 |
| `EXTERNAL_READ` | 允许受控目标；私网和未授权域名拒绝 |
| `LOCAL_WRITE` | 需要会话或路径范围授权 |
| `EXTERNAL_WRITE` | 每次审批或具名持久授权 |
| `PRIVILEGED` | 默认拒绝，管理员策略可开启 |

### 13.5 Permission Grant

```python
@dataclass(frozen=True, slots=True)
class ToolPermissionGrant:
    grant_id: str
    actor_id: str
    scope: str
    tool_name: str | None
    server_name: str | None
    argument_constraints: Mapping[str, object]
    expires_at: datetime | None
    created_from_approval_id: str
```

持久授权必须绑定 actor、scope 和参数约束，不能用一个全局布尔值代表“永远允许所有 Tool”。

---

## 14. 审批与可恢复执行

### 14.1 审批票据

```python
@dataclass(frozen=True, slots=True)
class ToolApprovalTicket:
    approval_id: str
    turn_id: str
    call: ToolCall
    tool: ToolDefinition
    argument_summary: Mapping[str, object]
    risk_summary: str
    requested_at: datetime
    expires_at: datetime
    allowed_decisions: tuple[str, ...]
    checkpoint_id: str
```

### 14.2 两种运行模式

#### 交互等待模式

适用于本地 CLI/TUI：

- `ApprovalCoordinatorPort.request()` 等待用户决策。
- 必须有超时和取消。
- 不得持有数据库事务、文件锁或 Tool 并发锁等待用户。

#### 持久挂起模式

适用于 MessageBus、Web、Telegram、Discord：

1. AgentLoop 在真正执行 Tool 前保存 Loop Checkpoint。
2. 创建 `ToolApprovalTicket`。
3. 设置 `ctx.status = WAITING_APPROVAL`。
4. 生成等待审批的 `TurnResult`。
5. Worker 发布审批请求消息。
6. 用户决策形成新的 `ApprovalDecisionEnvelope`。
7. Application Approval Service 加载 Checkpoint，并以原 `turn_id` 或明确的 continuation id 恢复。

### 14.3 Kernel 兼容调整

Kernel 不按 Phase 名称做审批分支，但必须尊重 `ctx.result.status`：

```python
if ctx.result is None:
    raise MissingTurnResultError()

ctx.status = ctx.result.status

if ctx.status == TurnStatus.COMPLETED:
    await emit(turn_completed(ctx))
elif ctx.status == TurnStatus.WAITING_APPROVAL:
    await emit(turn_waiting_approval(ctx))
elif ctx.status == TurnStatus.DENIED:
    await emit(turn_denied(ctx))
```

这是通用终态处理，不是 Tool 特例。

### 14.4 Checkpoint 内容

Checkpoint 至少保存：

- Registry version。
- Visible Tool 名称和 Schema hash。
- Model messages。
- Tool round。
- 已完成 Tool Call 与 Result 引用。
- 待审批 Tool Call。
- 使用量。
- 会话、actor、trace 和时间信息。

恢复时必须重新执行策略检查；不能因为旧 Checkpoint 曾允许就跳过当前策略。

---

## 15. Tool Result 治理

### 15.1 单结果预算

推荐默认值：

```yaml
tool_results:
  inline_soft_limit_chars: 12000
  inline_hard_limit_chars: 50000
  turn_total_limit_chars: 120000
  preview_head_chars: 6000
  preview_tail_chars: 3000
  artifact_ttl_days: 7
```

处理规则：

- 小于 soft limit：直接注入。
- soft 到 hard：结构化裁剪，保留头尾、错误行、匹配行和摘要。
- 超过 hard：完整输出写 Artifact Store，上下文只保留预览与引用。
- 同轮累计超过 turn limit：优先溢出最大的旧结果。

### 15.2 持久化提示格式

```text
<tool-output persisted="true" truncated="true">
Tool: grep_search
Artifact: artifact_01H...
Size: 438220 bytes
SHA256: ...
Preview:
...
Use read_artifact with artifact_id to inspect a specific range.
</tool-output>
```

不要向模型暴露底层绝对文件路径。

### 15.3 上下文完整性治理

每轮模型调用前执行：

1. 删除没有对应 Tool Call 的孤儿 Tool Result。
2. 对缺失 Result 的历史 Call 插入稳定占位符。
3. 旧 Tool Result 微压缩。
4. 重新应用总预算。
5. 保留最近 N 个完整 Tool Result。
6. 保留系统消息和当前用户消息。
7. 超出模型 Token 上限时裁剪最旧、价值最低的非系统内容。

### 15.4 多模态结果

- 图片、音频、视频、大型 JSON 不以内联 Base64 形式进入消息历史。
- 先进入 Artifact Store，再按模型能力转换为 image/file part。
- 模型不支持对应媒体时，注入文本占位和 Artifact 元数据。

---

## 16. 事件、审计与持久化记录

### 16.1 新增 Event Type

```text
TOOL_SELECTION_COMPLETED
TOOL_CALL_REQUESTED
TOOL_POLICY_EVALUATED
TOOL_APPROVAL_REQUESTED
TOOL_APPROVAL_RESOLVED
TOOL_CALL_STARTED
TOOL_PROGRESS
TOOL_CALL_COMPLETED
TOOL_CALL_FAILED
TOOL_RESULT_PERSISTED
MCP_SERVER_STATE_CHANGED
MCP_TOOL_LIST_CHANGED
```

事件不得携带：

- Secret 参数原文。
- 完整 stdout/stderr。
- OAuth Token。
- 系统 Prompt。
- Exception 对象。

### 16.2 ToolExecutionRecord

扩展现有记录：

```python
@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    call_id: str
    tool_name: str
    registry_version: int
    source_type: ToolSourceType
    source_provider: str
    risk: ToolRisk
    status: ToolResultStatus
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    arguments_hash: str
    result_artifact_ids: tuple[str, ...] = ()
    approval_id: str | None = None
    error_code: str | None = None
    policy_reason_code: str | None = None
```

### 16.3 审计原则

- 审计记录 append-only。
- 原始参数只在明确加密存储策略下保存；默认保存规范化 hash 和脱敏摘要。
- 外部写入必须记录目标、审批 ID 和结果。
- 审计写失败对高风险 Tool 应 fail-closed；对只读 Tool 可按部署策略 fail-open。

---

## 17. 文件系统 Tool

### 17.1 WorkspaceScope

所有路径先通过：

```python
class WorkspaceScopePort(Protocol):
    def resolve_read(self, path: str) -> ResolvedPath:
        ...

    def resolve_write(self, path: str) -> ResolvedPath:
        ...
```

必须防止：

- `..` 路径穿越。
- 符号链接逃逸。
- 大小写不敏感文件系统绕过。
- Windows UNC、设备路径和 ADS。
- `~` 展开到工作区外。
- 路径解析与使用之间的 TOCTOU。

### 17.2 写入规则

- 使用原子临时文件 + rename。
- 保留可配置备份或 diff。
- 单文件写入有大小限制。
- 并发修改同一文件使用异步锁。
- `edit_file` 必须校验旧文本唯一匹配或使用明确 range/hash。
- `apply_patch` 在应用前解析并验证所有目标路径。

### 17.3 建议内建 Tool

```text
read_file
read_artifact
list_dir
glob_search
grep_search
write_file
edit_file
apply_patch
```

---

## 18. Shell Tool 与沙箱

### 18.1 安全层级

Shell 不仅靠正则黑名单，应采用组合防御：

1. 参数化命令优先于 shell 字符串。
2. 命令 AST/Token 解析。
3. 安全命令白名单与危险模式拒绝。
4. Workspace 路径检查。
5. 网络目标检查。
6. 环境变量清理。
7. OS 沙箱。
8. 新进程组或 Job Object。
9. 超时、输出和进程数限制。
10. 进程树清理。

### 18.2 Shell 参数模型

```json
{
  "type": "object",
  "properties": {
    "command": {"type": "string", "minLength": 1},
    "cwd": {"type": "string"},
    "timeout_seconds": {
      "type": "integer",
      "minimum": 1,
      "maximum": 600
    },
    "env": {
      "type": "object",
      "additionalProperties": {"type": "string"}
    }
  },
  "required": ["command"],
  "additionalProperties": false
}
```

用户提供的 `env` 必须经过允许列表，不能覆盖安全关键变量。

### 18.3 必须拒绝或审批的行为

- 原始磁盘写入、格式化、分区。
- 系统关机、重启。
- 权限提升。
- 反向 Shell。
- Fork bomb。
- 删除工作区根或大范围递归删除。
- 下载后直接 pipe 到 shell。
- 读取 Secret 目录。
- 杀死 Agent 自身或父进程。
- 绕过沙箱的解释器嵌套。

### 18.4 平台沙箱

- Linux：Bubblewrap，工作区按策略只读/读写绑定，`/tmp` 为 tmpfs，系统目录只读，可选 seccomp。
- macOS：Seatbelt Profile，限制文件和网络。
- Windows：Job Object、受限 Token、进程树终止、路径与危险命令检测。
- 无可用 OS 沙箱时，默认禁用高风险 Shell，除非部署管理员显式允许降级模式。

---

## 19. 网络与 SSRF 防护

所有 Web Tool 和 HTTP MCP Transport 必须复用同一个 `NetworkPolicyPort`。

### 19.1 URL 校验

- 只允许配置中的 scheme，默认 `https`，可选 `http`。
- 拒绝用户名密码嵌入 URL。
- DNS 解析后检查所有 A/AAAA 地址。
- 拒绝 loopback、RFC1918、link-local、multicast、reserved、CGNAT、ULA。
- 每次重定向重新校验。
- 防止 DNS rebinding：连接 IP 必须是已验证 IP。
- 端口使用 allow/deny policy。
- `.local`、`.localhost` 和部署定义内网域名默认拒绝。

### 19.2 出站限制

- 最大响应体。
- 最大重定向次数。
- 连接和读取超时。
- Content-Type allowlist。
- 压缩炸弹防护。
- 禁止自动发送本地 Cookie 或云元数据凭据。

---

## 20. MCP 完整设计

### 20.1 MCPServerConfig

```python
@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    transport: Literal["stdio", "sse", "streamable_http"]
    enabled: bool = True

    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env_secret_refs: Mapping[str, str] = field(default_factory=dict)

    url: str | None = None
    headers_secret_refs: Mapping[str, str] = field(default_factory=dict)

    include_tools: frozenset[str] = frozenset({"*"})
    exclude_tools: frozenset[str] = frozenset()

    connect_timeout_seconds: float = 30.0
    tool_timeout_seconds: float = 120.0
    keepalive_seconds: float = 120.0
    max_reconnect_attempts: int = 5
    enabled_features: frozenset[str] = frozenset({"tools"})
```

Secret 只存引用，不把真实值写入配置对象、日志或 Registry Definition。

### 20.2 状态机

```text
DISABLED
DISCONNECTED
CONNECTING
CONNECTED
DEGRADED
BLOCKED
RECONNECTING
STOPPING
```

状态切换必须发事件并记录最后错误的安全摘要。

### 20.3 生命周期

1. Bootstrap 读取配置。
2. Manager 并行连接，但限制并发。
3. 完成 initialize 握手和 capabilities 校验。
4. 调用 `tools/list`。
5. 过滤 include/exclude。
6. 清理工具名和 Schema。
7. 创建 `MCPToolHandler`。
8. 原子替换该 Provider 在 Registry 中的工具。
9. 监听 `tools/list_changed`，防抖后刷新。
10. 关闭时先停止接收新调用，再等待或取消在途调用，最后关闭 Transport。

### 20.4 命名

```text
mcp_{server_slug}_{tool_slug}
```

规则：

- 非字母数字字符转换为 `_`。
- 连续 `_` 合并。
- 总长度最大 64。
- 发生截断时追加稳定 hash 后缀。
- 保存规范名到真实 MCP 名称的映射。

### 20.5 Schema 转换

- 拒绝远程 `$ref`。
- 将不兼容类型降级为可验证的 JSON Schema 子集。
- 缺失根 object 时包装。
- 统一 `additionalProperties` 策略。
- 对描述做长度限制和控制字符清理。
- 保存原始 Schema hash 以便诊断。

### 20.6 MCP 内容转换

支持：

- text → `TextContent`
- image → Artifact + `ImageContent`
- resource → Artifact 或受控文本预览
- structuredContent → `JsonContent`
- isError → 失败 `ToolResult`

未知 block 不得静默丢失，应转换为安全占位和诊断元数据。

### 20.7 MCP 安全

- HTTP/SSE 经过统一 SSRF 校验。
- stdio 命令和 cwd 经过管理员配置校验，不能由模型直接添加任意服务器。
- 子进程环境变量只注入明确 Secret 引用。
- MCP 返回内容视为不可信。
- Tool List Change 不能覆盖内建工具。
- Sampling 默认关闭；开启时必须单独限流、模型 allowlist 和 Token 上限。
- MCP Admin Tool（add/remove）默认不向模型暴露；个人部署如开启，必须是 `PRIVILEGED` 并要求审批。

### 20.8 热重载

配置变更使用“先连新、验证成功、原子替换、再关旧”的方式。连接失败时保留旧客户端，避免工具瞬时全部消失。

---

## 21. Secret 管理

提供 `SecretStorePort`：

```python
class SecretStorePort(Protocol):
    async def get(self, ref: str) -> str:
        ...
```

实现优先级：

1. OS Keychain。
2. 云 Secret Manager。
3. 加密文件存储，权限 `0600`。

必须实现统一 `SecretRedactor`：

- 已加载 Secret 的精确值替换。
- 常见 Token 格式检测。
- Authorization/Cookie/Header 字段脱敏。
- 环境变量名称 allowlist。
- 日志结构化字段递归清理。

Secret 不得进入：

- Tool Definition。
- Agent Event。
- Tool Result。
- Approval 参数摘要。
- 异常 safe_message。

---

## 22. 内建 Tool 清单与默认风险

| Tool | Kind | Risk | 默认可见 | 并发 |
|---|---|---|---|---|
| `tool_search` | SEARCH | READ_ONLY | 是 | PARALLEL_SAFE |
| `read_file` | READ | READ_ONLY | 是 | PARALLEL_SAFE |
| `read_artifact` | READ | READ_ONLY | 否 | PARALLEL_SAFE |
| `list_dir` | READ | READ_ONLY | 是 | PARALLEL_SAFE |
| `glob_search` | SEARCH | READ_ONLY | 否 | PARALLEL_SAFE |
| `grep_search` | SEARCH | READ_ONLY | 否 | PARALLEL_SAFE |
| `write_file` | EDIT | LOCAL_WRITE | 否 | SERIAL_PER_SESSION |
| `edit_file` | EDIT | LOCAL_WRITE | 否 | SERIAL_PER_SESSION |
| `apply_patch` | EDIT | LOCAL_WRITE | 否 | SERIAL_PER_SESSION |
| `shell` | EXECUTE | PRIVILEGED | 否 | EXCLUSIVE |
| `web_search` | SEARCH | EXTERNAL_READ | 否 | PARALLEL_SAFE |
| `web_fetch` | FETCH | EXTERNAL_READ | 否 | PARALLEL_SAFE |
| `recall_memory` | MEMORY | READ_ONLY | 否 | PARALLEL_SAFE |
| `memorize` | MEMORY | LOCAL_WRITE | 否 | SERIAL_PER_SESSION |
| `forget_memory` | MEMORY | LOCAL_WRITE | 否 | SERIAL_PER_SESSION |
| `send_message` | COMMUNICATE | EXTERNAL_WRITE | 否 | SERIAL_PER_SESSION |
| `get_current_time` | READ | READ_ONLY | 是 | PARALLEL_SAFE |

`spawn_agent`、定时任务、浏览器控制和自修改能力不应与基础 Tool 同时默认开启；应按独立 Toolset 和更高风险策略部署。

---

## 23. 配置格式

```yaml
tools:
  enabled: true
  default_toolsets:
    - personal_assistant
  conflict_policy: error
  max_visible_tools: 32
  max_schema_tokens: 12000
  max_tool_rounds: 8

  selection:
    recent_lru_size: 8
    search_top_k: 16

  execution:
    default_timeout_seconds: 60
    max_parallel_calls: 4
    argument_max_bytes: 262144
    exact_failure_warn_after: 2
    exact_failure_block_after: 4
    same_tool_failure_halt_after: 8

  results:
    inline_soft_limit_chars: 12000
    inline_hard_limit_chars: 50000
    turn_total_limit_chars: 120000
    preview_head_chars: 6000
    preview_tail_chars: 3000
    artifact_ttl_days: 7

  approvals:
    mode: durable_suspend
    ticket_ttl_minutes: 30
    allow_persistent_grants: true

  sandbox:
    required_for_shell: true
    restrict_to_workspace: true
    network_default: deny_private
    allowed_env_keys:
      - LANG
      - TERM

  builtin:
    shell:
      enabled: true
      max_timeout_seconds: 600
    web_fetch:
      enabled: true
      max_response_bytes: 5000000

mcp:
  enabled: true
  refresh_debounce_ms: 300
  servers:
    filesystem_docs:
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace/docs"]
      include_tools: ["*"]
      exclude_tools: []
      tool_timeout_seconds: 120

    remote_service:
      transport: streamable_http
      url: "https://mcp.example.com/mcp"
      headers_secret_refs:
        Authorization: "secret://mcp/remote_service/token"
      include_tools: ["search", "lookup"]
```

配置加载时必须做完整验证，不能把错误延迟到第一次 Tool Call。

---

## 24. Composition Root

```python
async def build_tool_system(settings: Settings) -> ToolSystem:
    artifact_store = FileArtifactStore(settings.artifacts)
    audit_repository = SqlToolAuditRepository(settings.database)
    approval_repository = SqlApprovalRepository(settings.database)
    checkpoint_repository = SqlLoopCheckpointRepository(settings.database)

    network_policy = DefaultNetworkPolicy(settings.network)
    workspace_scope = DefaultWorkspaceScope(settings.workspace)
    secret_store = build_secret_store(settings.secrets)
    secret_redactor = DefaultSecretRedactor(secret_store)
    sandbox = build_sandbox(settings.sandbox)

    builtin_provider = BuiltinToolProvider(
        workspace_scope=workspace_scope,
        network_policy=network_policy,
        sandbox=sandbox,
        artifact_store=artifact_store,
    )

    mcp_manager = MCPClientManager(
        configs=settings.mcp.servers,
        network_policy=network_policy,
        secret_store=secret_store,
    )
    mcp_provider = McpToolProvider(mcp_manager)

    registry = AtomicToolRegistry(
        conflict_policy=settings.tools.conflict_policy
    )

    for provider in (builtin_provider, mcp_provider):
        handlers = await provider.load()
        await registry.replace_provider_tools(
            provider_name=provider.name,
            handlers=handlers,
        )

    policy_engine = CompositeToolPolicyEngine(...)
    approval_coordinator = DurableApprovalCoordinator(
        approval_repository=approval_repository,
        checkpoint_repository=checkpoint_repository,
    )

    result_processor = DefaultToolResultProcessor(
        artifact_store=artifact_store,
        redactor=secret_redactor,
        settings=settings.tools.results,
    )

    orchestrator = DefaultToolOrchestrator(
        registry=registry,
        validator=JsonSchemaToolValidator(),
        policy_engine=policy_engine,
        approval_coordinator=approval_coordinator,
        rate_limiter=DefaultToolRateLimiter(),
        concurrency_controller=ToolConcurrencyController(),
        result_processor=result_processor,
        audit_repository=audit_repository,
    )

    catalog = DefaultToolCatalog(
        registry=registry,
        selector=HybridToolSelector(...),
        toolsets=settings.toolsets,
    )

    return ToolSystem(
        registry=registry,
        catalog=catalog,
        executor=orchestrator,
        providers=(builtin_provider, mcp_provider),
    )
```

Runtime Factory 只接收 Tool Port：

```python
ContextAssemblyPhase(tool_catalog=tool_system.catalog, ...)
AgentLoopPhase(tool_executor=tool_system.executor, ...)
```

`RuntimeKernel` 不接收 Registry、MCP Manager 或 Sandbox。

---

## 25. 数据持久化建议

### 25.1 tool_execution_records

```text
id
turn_id
call_id
actor_id
session_id
tool_name
registry_version
source_type
source_provider
risk
status
arguments_hash
arguments_redacted_json
approval_id
started_at
completed_at
duration_ms
error_code
policy_reason_code
artifact_ids_json
```

索引：

- unique `(turn_id, call_id)`
- `(session_id, started_at)`
- `(tool_name, status, started_at)`

### 25.2 tool_approval_tickets

```text
approval_id
turn_id
call_id
actor_id
status
risk_summary
arguments_redacted_json
checkpoint_id
requested_at
expires_at
resolved_at
resolved_by
decision
grant_id
```

### 25.3 tool_permission_grants

```text
grant_id
actor_id
scope
tool_name
server_name
constraints_json
created_at
expires_at
revoked_at
created_from_approval_id
```

### 25.4 tool_loop_checkpoints

Checkpoint 应加密或存储在受保护数据库中，并包含版本字段和完整性 hash。审批完成或过期后按保留策略清理。

### 25.5 mcp_server_state

仅保存配置引用、状态、最后连接时间、最后错误码和 Tool Schema hash；不保存 Token 明文。

---

## 26. 错误体系

```text
ToolError
├── ToolNotFoundError
├── ToolNotVisibleError
├── ToolArgumentParseError
├── ToolArgumentValidationError
├── ToolPolicyDeniedError
├── ToolApprovalRequiredError
├── ToolApprovalExpiredError
├── ToolRateLimitedError
├── ToolTimeoutError
├── ToolCancelledError
├── ToolExecutionError
├── ToolOutputValidationError
├── ToolResultPersistenceError
├── ToolSandboxError
├── ToolWorkspaceBoundaryError
├── ToolNetworkPolicyError
└── MCPError
    ├── MCPConnectionError
    ├── MCPProtocolError
    ├── MCPAuthenticationError
    ├── MCPToolListError
    └── MCPToolCallError
```

每个错误必须包含：

```python
code: str
safe_message: str
retryable: bool
```

内部异常堆栈只进入服务端日志，不进入 Tool Result、Agent Event 或 Channel。

---

## 27. 关键实现伪代码

### 27.1 单 Tool 执行

```python
async def execute(self, *, call, context, visible_tools):
    started = self._clock.now()
    definition = self._resolve_visible(call, visible_tools)

    try:
        raw_args = self._argument_parser.parse(call.arguments)
        args = self._coercer.coerce(raw_args, definition.input_schema)
        self._validator.validate(args, definition.input_schema)

        decision = await self._policy.evaluate(
            definition=definition,
            arguments=args,
            context=context,
        )

        if decision.type is PolicyDecisionType.DENY:
            return self._denied_result(call, definition, decision)

        if decision.type is PolicyDecisionType.REQUIRE_APPROVAL:
            return await self._approval.handle_required(
                call=call,
                definition=definition,
                arguments=args,
                context=context,
                decision=decision,
            )

        await self._rate_limiter.acquire(definition, context)

        async with self._concurrency.acquire(definition, context):
            handler = self._registry.resolve(
                definition.name,
                version=visible_tools.registry_version,
            )
            async with asyncio.timeout(definition.limits.timeout_seconds):
                raw_result = await handler.execute(
                    arguments=args,
                    context=context,
                )

        result = await self._result_processor.process(
            definition=definition,
            result=raw_result,
            context=context,
        )

        await self._audit.record_success(...)
        return result

    except asyncio.CancelledError:
        await self._audit.record_cancelled(...)
        raise
    except Exception as exc:
        mapped = self._error_mapper.map(exc)
        await self._audit.record_failure(...)
        return self._failed_result(call, definition, mapped, started)
```

### 27.2 Agent Loop Tool 分支

```python
while True:
    if ctx.tool_round >= ctx.max_tool_rounds:
        raise MaxToolRoundsExceededError()

    response = await model.generate(
        messages=ctx.model_messages,
        tools=ctx.available_tools,
    )
    ctx.model_responses.append(response)
    ctx.usage = ctx.usage + response.usage

    if not response.tool_calls:
        ctx.output_text = response.text or ""
        return

    calls = model_adapter.parse_tool_calls(response)
    repetition_guard.check(calls, ctx.tool_calls, ctx.tool_results)

    results = await tool_executor.execute_many(
        calls=calls,
        context=build_tool_context(ctx),
        visible_tools=require_visible_tools(ctx),
    )

    ctx.tool_round += 1
    ctx.tool_calls.extend(calls)
    ctx.tool_results.extend(results)

    if any(r.status is ToolResultStatus.APPROVAL_REQUIRED for r in results):
        checkpoint = await checkpoint_service.save(ctx)
        ctx.loop_checkpoint_id = checkpoint.checkpoint_id
        ctx.status = TurnStatus.WAITING_APPROVAL
        ctx.output_text = approval_message_builder.build(results)
        return

    ctx.model_messages.extend(
        tool_message_adapter.to_messages(calls, results)
    )
    context_governor.apply(ctx)
```

---

## 28. 测试策略

### 28.1 单元测试

必须覆盖：

- ToolDefinition 名称和 Schema 校验。
- Registry 原子替换。
- 同名冲突策略。
- Toolset 循环检测。
- Tool Selector 可见性、风险、scope 和预算。
- JSON 参数解析与类型转换。
- Schema 校验错误。
- Policy 合并最严格原则。
- 审批前不执行 Handler。
- 超时和取消传播。
- 并行与独占调度。
- 重复调用与失败循环保护。
- 空 Tool Result 规范化。
- 大结果 Artifact 化。
- Secret 脱敏。
- Tool message 完整性治理。

### 28.2 AgentLoop 集成测试

场景：

1. 无 Tool Call，直接完成。
2. 单个只读 Tool 成功。
3. 多个并行 Tool 成功。
4. 串行写 Tool。
5. Tool 参数无效后模型修正。
6. Tool 失败后模型给出解释。
7. 达到最大轮数。
8. 同参数重复失败被阻断。
9. 审批挂起并恢复。
10. Checkpoint 恢复后策略变化导致拒绝。
11. 大结果落盘后模型通过 `read_artifact` 分段读取。

### 28.3 安全测试

- 路径穿越和符号链接逃逸。
- Windows UNC、设备路径、ADS。
- `rm -rf`、fork bomb、reverse shell、pipe-to-shell。
- Agent 自杀命令。
- Secret 环境变量泄漏。
- SSRF 到 127.0.0.1、RFC1918、link-local、云 metadata。
- DNS rebinding 和恶意重定向。
- 压缩炸弹和超大响应。
- MCP 描述 Prompt Injection。
- MCP Tool 名冲突。
- MCP 返回恶意内容块。
- 审批票据重放、过期和跨 actor 使用。

### 28.4 MCP 集成测试

使用测试 MCP Server 覆盖：

- stdio、SSE、Streamable HTTP。
- initialize 和 capabilities。
- tools/list 与 tools/call。
- `tools/list_changed`。
- 断线重连。
- 新旧客户端原子切换。
- OAuth Token 过期。
- Tool Call 超时。
- 不合规 Schema 转换。
- 大结果和图片内容。

### 28.5 架构测试

确保：

- `runtime/` 不导入 `infrastructure.mcp`、具体 Sandbox 或 MessageBus。
- `domain/tools/` 不导入 `infrastructure/`。
- MCP Adapter 只通过 Tool Handler 协议进入 Registry。
- Channel Adapter 不直接调用 Tool Handler。
- Tool Handler 不访问全局 Service Locator。
- 模块 import 不创建连接或后台任务。

### 28.6 性能与可靠性测试

- 1,000 个 Tool Definition 的选择与 Schema 构建。
- 100 个 MCP Server 配置的并发连接限制。
- 超大 Tool Result 的内存峰值。
- Registry 热更新期间并发执行。
- 取消后无遗留子进程。
- Artifact 清理和 Checkpoint 清理。

---

## 29. 可观测性指标

建议指标：

```text
tool_calls_total{tool,status,source,risk}
tool_call_duration_seconds{tool}
tool_argument_validation_failures_total{tool}
tool_policy_decisions_total{tool,decision,reason}
tool_approvals_total{tool,decision}
tool_result_bytes{tool}
tool_results_persisted_total{tool}
tool_timeouts_total{tool}
tool_cancellations_total{tool}
tool_repetition_blocks_total{tool}
mcp_server_state{server,state}
mcp_reconnects_total{server}
mcp_tool_refresh_total{server,status}
sandbox_denials_total{reason}
ssrf_denials_total{reason}
```

Trace Span 至少包含：

- tool name。
- source/provider。
- registry version。
- risk。
- status。
- duration。
- argument hash。
- artifact count。

不得把参数原文和 Result 原文默认写入 Span。

---

## 30. 实现顺序

该顺序是完整系统的依赖顺序，不代表删减功能版本：

1. Tool 领域模型、错误和 Port。
2. Registry、Definition 校验和 Provider 协议。
3. Catalog、Toolset、Selector 和模型 Schema Adapter。
4. Orchestrator 基础执行链、参数校验和结果模型。
5. Policy、Approval、Audit、Checkpoint。
6. Result Processor、Artifact Store 和上下文治理。
7. 文件系统 Tool 与 WorkspaceScope。
8. 网络 Policy、Web Tool 和 SSRF 防护。
9. Shell Tool 与跨平台 Sandbox。
10. MCP Client、Manager、Adapter、热刷新和 OAuth。
11. ContextAssemblyPhase 与 AgentLoopPhase 集成。
12. PersistencePhase 保存 Tool Record、Approval 和 Checkpoint。
13. MessageBus 审批消息与恢复入口。
14. 完整测试、指标、故障注入和安全审计。

每一步都必须保留完整领域接口，禁止先写临时 `dict[str, Any]` 再长期遗留。

---

## 31. 验收标准

### 架构

- [ ] Tool 不成为 Kernel 硬编码分支。
- [ ] Tool 可见性在 ContextAssembly 计算。
- [ ] Tool 执行在 AgentLoop 内通过 Port 完成。
- [ ] Registry、Catalog、Orchestrator、MCP Manager 职责分离。
- [ ] Runtime 和 Domain 不导入具体 MCP/Sandbox/DB 实现。
- [ ] 所有 Provider 由 Composition Root 显式加载。

### 正确性

- [ ] 模型看到的 Schema 与执行时 Registry version 一致。
- [ ] Tool 参数经过严格解析、强制转换和 Schema 校验。
- [ ] Tool Call 只能调用当前 VisibleToolSet 中的工具。
- [ ] 多 Tool Call 按并发模式正确执行。
- [ ] 最大轮数和重复失败保护生效。
- [ ] Tool Result 能正确注入下一轮模型上下文。

### 安全

- [ ] 所有副作用 Tool 在执行前经过 Policy。
- [ ] 需要审批时绝不提前执行。
- [ ] 审批票据绑定 actor、turn、call 和 checkpoint。
- [ ] 文件路径无法逃逸 Workspace。
- [ ] Shell 有 OS 沙箱或默认禁用高风险模式。
- [ ] Web/MCP HTTP 请求统一经过 SSRF 防护。
- [ ] Secret 不进入日志、事件、结果和审批摘要。
- [ ] MCP Tool 不能覆盖内建 Tool。

### 结果治理

- [ ] 单结果和单轮总预算均有限制。
- [ ] 大结果完整持久化并返回 Artifact 引用。
- [ ] 多模态内容不以内联 Base64 污染上下文。
- [ ] 孤儿结果、缺失结果和旧结果得到治理。

### MCP

- [ ] 支持 stdio、SSE 和 Streamable HTTP。
- [ ] 支持连接状态机、重连和超时。
- [ ] 支持 Tool List Changed 原子刷新。
- [ ] 支持 include/exclude 和名称净化。
- [ ] 支持 Secret 引用和 OAuth Token 管理。
- [ ] HTTP MCP 每次重定向重新做 URL 校验。

### 运行与测试

- [ ] 成功、失败、拒绝、审批挂起、取消和超时均有稳定结果。
- [ ] EventSink 故障不破坏 Tool 执行；安全审计故障按风险策略处理。
- [ ] 取消后无残留子进程、锁和临时资源。
- [ ] 单元、集成、安全、架构和恢复测试全部通过。
- [ ] 类型检查、lint 和依赖边界检查通过。

---

## 32. 最终边界总结

```text
RuntimeKernel
  只编排 Phase

ContextAssemblyPhase
  通过 ToolCatalogPort 获取当前可见 Tool Schema

AgentLoopPhase
  通过 ModelPort 与 ToolExecutorPort 完成循环

ToolRegistry
  保存版本化 Tool Definition + Handler 快照

ToolCatalog
  负责 Toolset、权限预过滤、搜索和可见性

ToolOrchestrator
  负责校验、策略、审批、限流、并发、执行、结果治理和审计

MCPManager
  负责连接、协议、重连、刷新和 Adapter

Sandbox / NetworkPolicy / WorkspaceScope
  负责真实安全边界

ArtifactStore / Audit / Checkpoint
  负责大结果、审计和可恢复执行
```

该设计使 Cogito-Agent 可以从少量个人助理工具扩展到大量内建 Tool、插件 Tool 和 MCP Tool，同时保持 Kernel 简洁、Phase 顺序显式、权限可审计、结果可治理、执行可恢复、安全边界可验证。
