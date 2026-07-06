# Cogito-Agent `AgentLoopPhase` 最终实现规格

> 本文档定义 `AgentLoopPhase` 的完整实现路径与稳定接口。实现完成后，该 Phase 应能够在 Channel 无关、MessageBus 无关、模型供应商无关的前提下，可靠执行“模型调用 → 工具调用 → 工具结果回填 → 再次模型调用”，并覆盖流式输出、工具策略、人工审批、超时、取消、循环检测、上下文窗口、错误隔离、用量汇总、事件观测和恢复执行。
>
> 文中定义的模型、Port、状态机和验收标准均按长期运行时边界设计，不保留临时接口或占位实现。

---

## 1. 目标与结论

`AgentLoopPhase` 是八阶段 Pipeline 中唯一负责“推理与行动闭环”的阶段：

```text
ContextAssemblyPhase
        │
        │ model_messages + available_tools
        ▼
AgentLoopPhase
        │
        ├── Model Stream
        ├── Tool Calls
        ├── Tool Policy / Approval
        ├── Tool Execution
        ├── Loop Guard
        └── Final Assistant Response
        │
        ▼
KnowledgeExtractionPhase
```

最终实现必须满足以下结论：

1. `AgentLoopPhase` 是无状态组件，可以被多个 Turn 重复使用；所有单轮状态进入 `TurnContext`。
2. Phase 只依赖抽象 Port，不导入模型 SDK、工具实现、数据库、MessageBus 或 Channel 类型。
3. 模型侧只提供一套统一流式接口；非流式模型由 Adapter 包装成同一事件流，避免维护两套 Agent Loop。
4. 每次模型输出只能是“最终文本”或“工具调用集合”之一；混合输出视为协议错误。
5. 工具执行前必须完成名称解析、参数验证、策略判定、审批判定和预算检查。
6. 同一模型轮返回的工具调用先整体完成策略判定；存在待审批调用时，该批次不执行任何真实工具，避免部分副作用。
7. 工具可以在安全条件下受限并发执行，但结果必须按模型声明顺序回填，保证上下文确定性。
8. 所有工具调用都有稳定幂等键；可产生副作用的 Adapter 必须使用该幂等键。
9. 模型轮数、工具轮数、工具总调用数、重复调用、总耗时和单次耗时都有独立上限。
10. `asyncio.CancelledError` 必须原样向上抛出，任何包装层都不得将其转换为普通业务错误。
11. 原始异常、密钥、系统 Prompt、完整工具参数和敏感工具结果不得进入公开事件。
12. `AgentLoopPhase` 不直接持久化；待审批状态、工具记录、最终回复和检查点只写入 `TurnContext`，由 `PersistencePhase` 统一提交。

---

## 2. 职责边界

### 2.1 `AgentLoopPhase` 负责

- 在每次模型调用前检查取消和剩余时间。
- 根据上下文窗口限制生成本次模型调用视图。
- 调用统一的流式 `ModelPort`。
- 聚合模型文本增量、工具调用增量、结束原因和 Usage。
- 校验模型输出协议。
- 向外发送安全的模型与工具生命周期事件。
- 解析并验证工具调用参数。
- 检查工具可见性、授权、风险、审批要求和执行预算。
- 执行工具并把工具结果转换为标准 `ToolMessage`。
- 记录工具执行结果和时延。
- 检测重复工具调用与周期性工具循环。
- 在取得最终回答后设置 `ctx.output_text` 和 `ctx.final_response`。
- 在需要人工审批时设置 `ctx.pending_approval`、`ctx.loop_checkpoint` 和 `WAITING_APPROVAL` 状态。

### 2.2 `AgentLoopPhase` 不负责

- 加载历史、偏好、长期记忆或 Session。
- 执行关键词、向量或长期记忆检索。
- 生成系统 Prompt 或选择可见工具。
- 从最终回答中提取偏好、事实或记忆。
- 保存消息、工具记录、审批请求或检查点。
- 发布 MessageBus Envelope。
- 把供应商原生对象暴露给 `TurnContext`。
- 实现模型 SDK 重试、HTTP 重试、连接池或熔断器。
- 实现工具具体业务逻辑。

### 2.3 依赖方向

```text
AgentLoopPhase
    ├── ModelPort
    ├── ModelContextWindowPort
    ├── ToolRegistryPort
    ├── ToolPolicyPort
    ├── ToolExecutorPort
    ├── ClockPort
    └── TurnEventEmitter（由 Kernel 创建并绑定到 TurnContext）

Infrastructure Adapters
    ├── OpenAI / Anthropic / Local Model Adapter → ModelPort
    ├── Tool Registry Adapter                 → ToolRegistryPort
    ├── Policy Adapter                        → ToolPolicyPort
    └── Tool Runtime Adapter                  → ToolExecutorPort
```

禁止以下依赖：

```text
AgentLoopPhase → OpenAI SDK / Anthropic SDK / HTTP client
AgentLoopPhase → Redis / SQLAlchemy / NATS / Kafka
AgentLoopPhase → Telegram / Discord / FastAPI
AgentLoopPhase → PersistencePhase
AgentLoopPhase → KnowledgeExtractionPhase
Tool Adapter   → TurnContext
```

---

## 3. 运行状态机

### 3.1 Phase 级状态机

```text
┌──────────────┐
│ INITIALIZING │
└──────┬───────┘
       ▼
┌────────────────────┐
│ PREPARE_MODEL_CALL │◄────────────────────────────┐
└─────────┬──────────┘                             │
          ▼                                        │
┌────────────────────┐                             │
│ STREAM_MODEL       │                             │
└─────────┬──────────┘                             │
          ▼                                        │
┌────────────────────┐                             │
│ VALIDATE_OUTPUT    │                             │
└──────┬───────┬─────┘                             │
       │       │                                   │
       │ text  │ tool calls                        │
       ▼       ▼                                   │
┌────────────┐ ┌────────────────────┐              │
│ FINALIZE   │ │ PREPARE_TOOL_BATCH │              │
└─────┬──────┘ └─────────┬──────────┘              │
      │                  ▼                         │
      │        ┌────────────────────┐              │
      │        │ POLICY_EVALUATION  │              │
      │        └──────┬───────┬─────┘              │
      │               │       │                    │
      │         approval      execute              │
      │               │       ▼                    │
      │               │ ┌────────────────────┐     │
      │               │ │ EXECUTE_TOOL_BATCH │     │
      │               │ └─────────┬──────────┘     │
      │               │           ▼                │
      │               │ ┌────────────────────┐     │
      │               │ │ APPEND_TOOL_RESULTS│─────┘
      │               │ └────────────────────┘
      │               ▼
      │      ┌─────────────────────┐
      │      │ SUSPEND_FOR_APPROVAL│
      │      └──────────┬──────────┘
      ▼                 ▼
┌────────────────────────────┐
│ RETURN TO NEXT PIPELINE PHASE│
└────────────────────────────┘
```

### 3.2 单次模型轮输出模式

每次模型调用有独立的 `ModelRoundMode`：

```python
class ModelRoundMode(StrEnum):
    UNKNOWN = "unknown"
    FINAL_RESPONSE = "final_response"
    TOOL_CALLS = "tool_calls"
```

判定规则：

- 收到第一个非空文本增量后，模式从 `UNKNOWN` 变为 `FINAL_RESPONSE`。
- 收到第一个工具调用增量后，模式从 `UNKNOWN` 变为 `TOOL_CALLS`。
- Usage、响应 ID、心跳等控制事件不改变模式。
- 已进入 `FINAL_RESPONSE` 后再收到工具调用增量，抛 `MixedModelOutputError`。
- 已进入 `TOOL_CALLS` 后再收到非空文本增量，抛 `MixedModelOutputError`。
- 模型结束时仍为 `UNKNOWN`，抛 `EmptyModelOutputError`。

该约束使最终文本可以安全实时输出，同时避免把工具规划阶段的中间文本错误展示给用户。

---

## 4. 推荐目录结构

```text
cogito_agent/
├── runtime/
│   ├── context.py
│   ├── errors.py
│   ├── events.py
│   ├── models.py
│   └── phases/
│       └── agent_loop.py
│
├── domain/
│   ├── messages.py
│   ├── model.py
│   ├── tools.py
│   ├── approval.py
│   └── usage.py
│
├── ports/
│   ├── model.py
│   ├── model_context.py
│   ├── tools.py
│   ├── tool_policy.py
│   ├── clock.py
│   └── events.py
│
├── runtime/agent_loop/
│   ├── __init__.py
│   ├── assembler.py
│   ├── batch_executor.py
│   ├── loop_guard.py
│   ├── protocol_validator.py
│   └── usage_accumulator.py
│
└── tests/
    ├── unit/runtime/phases/
    │   ├── test_agent_loop_final_response.py
    │   ├── test_agent_loop_tools.py
    │   ├── test_agent_loop_streaming.py
    │   ├── test_agent_loop_policy.py
    │   ├── test_agent_loop_approval.py
    │   ├── test_agent_loop_limits.py
    │   ├── test_agent_loop_timeout.py
    │   ├── test_agent_loop_cancellation.py
    │   └── test_agent_loop_events.py
    └── architecture/
        └── test_agent_loop_dependencies.py
```

`agent_loop.py` 只保留主编排逻辑；流聚合、循环检测、批量执行和 Usage 汇总分别由内部组件承担，避免形成不可测试的超大方法。

---

## 5. 领域模型

## 5.1 强类型消息模型

原始框架中的通用 `ModelMessage` 应替换为角色约束明确的联合类型：

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, TypeAlias


@dataclass(frozen=True, slots=True)
class SystemMessage:
    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserMessage:
    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    content: str | None = None
    tool_calls: tuple["ToolCall", ...] = ()
    provider_response_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        has_text = bool(self.content and self.content.strip())
        has_tools = bool(self.tool_calls)
        if has_text == has_tools:
            raise ValueError(
                "AssistantMessage must contain exactly one of content or tool_calls"
            )


@dataclass(frozen=True, slots=True)
class ToolMessage:
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)


ModelMessage: TypeAlias = (
    SystemMessage | UserMessage | AssistantMessage | ToolMessage
)
```

约束：

- `AssistantMessage` 必须且只能包含最终文本或工具调用。
- `ToolMessage.tool_call_id` 必须对应前一个 `AssistantMessage` 中存在的调用。
- `ToolMessage.content` 是经过安全序列化和长度限制的模型可见结果，不是任意 Python 对象。
- 供应商原生消息只存在于 Adapter 内部。

## 5.2 工具定义

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class ToolSideEffect(StrEnum):
    NONE = "none"
    LOCAL_MUTATION = "local_mutation"
    EXTERNAL_MUTATION = "external_mutation"


class ToolRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, object]
    side_effect: ToolSideEffect
    risk_level: ToolRiskLevel
    timeout_seconds: float
    idempotent: bool
    parallel_safe: bool
    max_result_chars: int = 32_000
    metadata: Mapping[str, object] = field(default_factory=dict)
```

要求：

- `name` 在本轮 `available_tools` 中唯一。
- `input_schema` 使用 JSON Schema 子集，由 Registry 或专用 Validator 统一校验。
- `parallel_safe=True` 只表示工具实现允许并发，不代表策略一定允许并发。
- 有外部副作用且非幂等的工具不得自动重试。

## 5.3 工具调用

```python
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool_name: str
    arguments: Mapping[str, object]
    arguments_json: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    call: ToolCall
    definition: ToolDefinition
    idempotency_key: str
    arguments_fingerprint: str


@dataclass(frozen=True, slots=True)
class RejectedToolCall:
    call: ToolCall
    arguments_fingerprint: str
    error_code: str
    safe_message: str


@dataclass(frozen=True, slots=True)
class ToolCallPlan:
    original_calls: tuple[ToolCall, ...]
    executable_calls: tuple[PreparedToolCall, ...]
    rejected_calls: tuple[RejectedToolCall, ...]
```

`ToolCallPlan` 保留模型原始顺序，同时把可执行调用与准备阶段拒绝的调用分开：

- 未知工具、参数 JSON 错误和 Schema 错误进入 `rejected_calls`。
- `rejected_calls` 不进入 Policy 或 Executor，而是在结果合并时生成标准错误 `ToolMessage`。
- 循环检测和预算统计基于 `original_calls`，因此模型不能通过持续产生非法调用绕过限制。

规则：

- `call_id` 在单个 Turn 内必须唯一。
- `ordinal` 是模型返回顺序，从 0 开始连续递增。
- `arguments_json` 保存规范化前的完整 JSON 字符串，仅用于内部诊断和必要持久化，不进入公开事件。
- `arguments` 只能是 JSON 对象；数组、字符串或标量根节点均视为参数协议错误。
- `idempotency_key` 推荐格式：`{turn_id}:{call_id}`。
- `arguments_fingerprint` 使用工具名和规范化 JSON 计算 SHA-256，用于循环检测。

## 5.4 工具执行结果

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class ToolExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolArtifactRef:
    artifact_id: str
    media_type: str
    name: str | None = None
    uri: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    status: ToolExecutionStatus
    model_content: str
    safe_message: str | None = None
    error_code: str | None = None
    retryable: bool = False
    artifacts: tuple[ToolArtifactRef, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
```

`model_content` 必须由 Tool Adapter 生成，遵守以下要求：

- 是 UTF-8 文本或紧凑 JSON 字符串。
- 不包含异常堆栈、凭据、访问令牌或内部连接信息。
- 大结果应转存为 Artifact，仅返回摘要和引用。
- 超过 `ToolDefinition.max_result_chars` 时由 AgentLoop 截断，并添加明确的截断标记。

## 5.5 模型调用请求

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelInvocationRequest:
    turn_id: str
    request_id: str
    round_index: int
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    timeout_seconds: float
    max_output_tokens: int
```

模型名称、温度、供应商路由、采样参数等应在 Model Adapter 或独立 Model Routing 配置中确定，不通过 `metadata` 临时传递。

## 5.6 模型流事件

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


class ModelFinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelToolCallDelta:
    ordinal: int
    call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str = ""


@dataclass(frozen=True, slots=True)
class ModelUsageUpdate:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelCompleted:
    finish_reason: ModelFinishReason
    provider_response_id: str | None = None


ModelStreamEvent: TypeAlias = (
    ModelTextDelta
    | ModelToolCallDelta
    | ModelUsageUpdate
    | ModelCompleted
)
```

Adapter 责任：

- 把供应商原生流事件转换成以上类型。
- 保证工具调用 `ordinal` 稳定。
- 不把 reasoning token、隐藏思维链或供应商内部字段暴露为文本增量。
- 发生供应商协议错误时抛稳定的 Model Adapter 异常。

Phase 责任：

- 聚合 `arguments_delta`。
- 解析完整 JSON。
- 检查结束原因与实际输出一致。
- 汇总 Usage。

## 5.7 模型轮结果

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelRoundOutput:
    round_index: int
    finish_reason: ModelFinishReason
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    provider_response_id: str | None
    input_tokens: int
    output_tokens: int
```

必须满足：

```text
text 非空 XOR tool_calls 非空
```

---

## 6. `TurnContext` 必要调整

原框架中以下字段仍为 `object` 或不足以表达 Agent Loop 的完整状态，必须正式类型化：

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class TurnContext:
    # 已有字段略

    model_messages: list[ModelMessage] = field(default_factory=list)
    available_tools: list[ToolDefinition] = field(default_factory=list)

    model_responses: list[ModelRoundOutput] = field(default_factory=list)
    final_response: AssistantMessage | None = None
    output_text: str | None = None

    tool_records: list[ToolExecutionRecord] = field(default_factory=list)
    usage: UsageSummary = field(default_factory=UsageSummary)

    model_calls_used: int = 0
    tool_rounds_used: int = 0
    total_tool_calls_used: int = 0

    deadline_at: datetime | None = None
    cancellation_token: CancellationToken | None = None
    event_emitter: TurnEventEmitter | None = field(default=None, repr=False)

    pending_approval: PendingApprovalBatch | None = None
    loop_checkpoint: AgentLoopCheckpoint | None = None
```

### 6.1 为什么需要 `TurnEventEmitter`

Kernel 支持每次 `run()` 传入不同的 `AgentEventSink`，因此不能把固定 Sink 注入可复用的 `AgentLoopPhase`。Kernel 应在创建 `TurnContext` 后绑定一个 Turn 级安全事件发射器：

```python
class TurnEventEmitter(Protocol):
    async def emit(
        self,
        event_type: AgentEventType,
        *,
        phase: str | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        ...
```

该对象负责：

- 自动补齐 `turn_id`、`request_id`、时间戳。
- 隔离 EventSink 异常。
- 过滤禁止字段。
- 统一附加 Trace 属性。

Phase 不应直接持有原始 `AgentEventSink`，也不应自己构造时间戳。

### 6.2 取消令牌

```python
class CancellationToken(Protocol):
    @property
    def is_cancelled(self) -> bool:
        ...

    def raise_if_cancelled(self) -> None:
        ...
```

`raise_if_cancelled()` 必须抛 `asyncio.CancelledError`，不得抛普通 Runtime Error。

---

## 7. Port 接口

## 7.1 `ModelPort`

```python
from collections.abc import AsyncIterator
from typing import Protocol


class ModelPort(Protocol):
    def stream(
        self,
        request: ModelInvocationRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        ...
```

只保留 `stream()`：

- 原生流式模型直接映射。
- 非流式模型 Adapter 在响应完成后依次产生文本、工具调用、Usage 和 Completed 事件。
- AgentLoop 不出现 `if model.supports_streaming` 分支。

## 7.2 `ModelContextWindowPort`

Agent Loop 每次回填工具结果后，消息数量会继续增长，不能只依赖 `ContextAssemblyPhase` 的初次预算。

```python
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ContextWindowRequest:
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    reserved_output_tokens: int


class ModelContextWindowPort(Protocol):
    async def fit(
        self,
        request: ContextWindowRequest,
    ) -> tuple[ModelMessage, ...]:
        ...
```

要求：

- 返回本次模型调用视图，不修改 `ctx.model_messages` 的规范记录。
- 可以压缩或替换过大的旧工具结果，但不得删除当前用户输入、核心系统规则或尚未匹配的工具调用。
- 无法在窗口内保留必要消息时抛 `ContextWindowExceededError`。
- 不在此 Port 内执行外部检索。

## 7.3 `ToolRegistryPort`

虽然 `ContextAssemblyPhase` 已生成 `available_tools`，AgentLoop 仍应通过稳定索引解析名称并执行参数验证：

```python
class ToolRegistryPort(Protocol):
    def resolve(
        self,
        *,
        name: str,
        available_tools: tuple[ToolDefinition, ...],
    ) -> ToolDefinition | None:
        ...

    def validate_arguments(
        self,
        *,
        definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> None:
        ...
```

未知工具和参数错误属于可安全反馈给模型的工具级错误，不直接暴露内部异常。

## 7.4 `ToolPolicyPort`

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ToolPolicyDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    decision: ToolPolicyDecisionType
    reason_code: str
    safe_message: str
    approval_prompt: str | None = None


class ToolPolicyPort(Protocol):
    async def evaluate(
        self,
        *,
        actor_id: str,
        session_id: str,
        prepared_call: PreparedToolCall,
    ) -> ToolPolicyDecision:
        ...
```

规则：

- AgentLoop 不根据工具名字硬编码权限。
- Policy 不执行工具。
- `DENY` 可转成标准工具错误并回填模型，让模型给出自然语言解释。
- `REQUIRE_APPROVAL` 会暂停整个工具批次。

## 7.5 `ToolExecutorPort`

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    turn_id: str
    request_id: str
    session_id: str
    actor_id: str
    call_id: str
    idempotency_key: str
    deadline_at: datetime | None


class ToolExecutorPort(Protocol):
    async def execute(
        self,
        *,
        prepared_call: PreparedToolCall,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        ...
```

要求：

- Executor 不接收完整 `TurnContext`。
- 有副作用工具必须识别 `idempotency_key`。
- Adapter 内部可以使用重试和熔断，但非幂等副作用工具不得透明重试。
- Executor 必须把可预期业务失败转换成 `ToolExecutionResult`，基础设施崩溃才抛异常。
- Executor 不发送 AgentEvent，由 AgentLoop 统一发送。

---

## 8. 配置模型

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    model_call_timeout_seconds: float = 90.0
    default_tool_timeout_seconds: float = 30.0
    max_tool_rounds: int = 8
    max_total_tool_calls: int = 24
    max_tool_calls_per_round: int = 8
    max_parallel_tools: int = 4
    max_output_tokens: int = 4_096
    max_repeated_fingerprint: int = 2
    cycle_detection_window: int = 8
    max_model_text_chars: int = 200_000
    approval_message: str = "该操作需要确认后才能继续。"
```

配置语义：

- `max_tool_rounds`：包含至少一个工具调用的模型轮上限，不是模型调用总数。
- 最大模型调用数理论上为 `max_tool_rounds + 1`；最后一次用于生成最终回答。
- `max_total_tool_calls`：整个 Turn 接受的工具调用总数上限。
- `max_tool_calls_per_round`：单个模型轮返回的工具调用数量上限。
- `max_parallel_tools`：仅对满足并发条件的工具生效。
- Phase 构造时验证所有数值大于零且约束一致。

`TurnInitPhase` 可以覆盖 Turn 级预算，但应写入明确字段，而不是任意 metadata。最终有效限制取系统限制与 Turn 限制中的更严格值。

---

## 9. 构造函数

```python
class AgentLoopPhase(BasePhase):
    name = "agent_loop"

    def __init__(
        self,
        *,
        model: ModelPort,
        context_window: ModelContextWindowPort,
        tool_registry: ToolRegistryPort,
        tool_policy: ToolPolicyPort,
        tool_executor: ToolExecutorPort,
        clock: ClockPort,
        config: AgentLoopConfig,
        assembler_factory: ModelResponseAssemblerFactory | None = None,
        loop_guard_factory: ToolLoopGuardFactory | None = None,
    ) -> None:
        ...
```

构造函数只保存不可变依赖，不保存：

- 当前 Turn。
- 当前模型轮。
- 已执行工具。
- Usage。
- EventSink。
- 临时字符串缓冲区。

因此该 Phase 可以注册为进程级单例。

---

## 10. 主执行算法

## 10.1 前置条件

进入 `execute()` 时必须验证：

1. `ctx.turn_id` 已存在。
2. `ctx.status == RUNNING`，或当前是合法的审批恢复状态。
3. `ctx.model_messages` 非空。
4. 至少包含一个 `UserMessage`。
5. `ctx.output_text is None`。
6. `available_tools` 名称无重复。
7. `max_tool_rounds`、deadline 和取消令牌已初始化。
8. 若存在 `loop_checkpoint`，其 actor、session 和校验摘要与当前请求一致。

不满足时抛 `InvalidAgentLoopStateError`。

## 10.2 主循环伪代码

```python
async def execute(self, ctx: TurnContext) -> None:
    self._validate_entry_state(ctx)

    loop_guard = self._loop_guard_factory.create(
        max_repeated_fingerprint=self._effective_repeat_limit(ctx),
        cycle_window=self._config.cycle_detection_window,
    )

    await self._resume_approved_batch_if_needed(ctx, loop_guard)

    while True:
        self._raise_if_cancelled(ctx)
        self._raise_if_deadline_exceeded(ctx)
        self._raise_if_model_call_budget_exhausted(ctx)

        round_index = ctx.model_calls_used
        invocation_messages = await self._build_invocation_messages(ctx)
        timeout_seconds = self._remaining_call_timeout(
            ctx,
            configured=self._config.model_call_timeout_seconds,
        )

        request = ModelInvocationRequest(
            turn_id=self._require_turn_id(ctx),
            request_id=ctx.request.request_id,
            round_index=round_index,
            messages=invocation_messages,
            tools=tuple(ctx.available_tools),
            timeout_seconds=timeout_seconds,
            max_output_tokens=self._config.max_output_tokens,
        )

        output = await self._invoke_model(ctx, request)
        ctx.model_calls_used += 1
        ctx.model_responses.append(output)
        self._accumulate_model_usage(ctx, output)

        if output.text is not None:
            final_message = AssistantMessage(
                content=output.text,
                provider_response_id=output.provider_response_id,
            )
            ctx.model_messages.append(final_message)
            ctx.final_response = final_message
            ctx.output_text = output.text
            return

        self._check_tool_budgets(ctx, output.tool_calls)
        plan = self._prepare_tool_plan(ctx, output.tool_calls)
        loop_guard.check_batch(plan)

        assistant_tool_message = AssistantMessage(
            tool_calls=plan.original_calls,
            provider_response_id=output.provider_response_id,
        )
        ctx.model_messages.append(assistant_tool_message)

        decisions = await self._evaluate_tool_policies(
            ctx,
            plan.executable_calls,
        )

        if self._requires_approval(decisions):
            self._suspend_for_approval(ctx, plan, decisions)
            return

        results = await self._execute_tool_plan(
            ctx=ctx,
            plan=plan,
            decisions=decisions,
        )

        self._append_tool_results(ctx, plan.original_calls, results)
        ctx.tool_rounds_used += 1
        ctx.total_tool_calls_used += len(plan.original_calls)
        loop_guard.record_batch(plan, results)
```

### 10.3 预算检查顺序

必须在执行任何真实工具前完成以下检查：

```text
模型输出协议
  → 单轮工具数量
  → 工具总数量
  → 工具轮数
  → 工具名称
  → 参数 JSON
  → 参数 Schema
  → 重复 call_id
  → 循环检测
  → Policy / Approval
  → 剩余时间
  → 工具执行
```

不能先执行第一个工具，再发现同批次后续调用需要审批或已经超预算。

---

## 11. 模型流处理

## 11.1 `ModelResponseAssembler`

每次模型调用创建一个新的 Assembler，职责如下：

- 维护 `ModelRoundMode`。
- 聚合最终文本。
- 按 `ordinal` 聚合工具名、call ID 和参数 JSON 增量。
- 拒绝重复 `ModelCompleted`。
- 拒绝 Completed 后继续产生语义事件。
- 检查文本长度上限。
- 汇总 Usage。
- 生成 `ModelRoundOutput`。

建议接口：

```python
class ModelResponseAssembler:
    def accept(self, event: ModelStreamEvent) -> None:
        ...

    def build(self, *, round_index: int) -> ModelRoundOutput:
        ...
```

## 11.2 流式事件发送

`MODEL_CALL_STARTED` 在调用前发送：

```json
{
  "round_index": 0,
  "message_count": 12,
  "tool_count": 5
}
```

进入 `FINAL_RESPONSE` 模式后，每个非空文本增量发送 `MODEL_DELTA`：

```json
{
  "round_index": 2,
  "sequence": 17,
  "text": "...",
  "provisional": true
}
```

所有流式增量在 `TURN_COMPLETED` 前都属于暂态展示数据。Channel 可以实时渲染，但必须在 Turn 失败、取消或模型输出被判定为截断/协议错误时撤销或标记未完成，不能把已收到增量视为持久化成功结果。

注意：

- `MODEL_DELTA` 仅包含可直接展示的最终回答文本。
- 不发送模型隐藏推理、内部分析或供应商 reasoning 字段。
- 工具调用模式不发送文本增量。
- EventSink 失败由 `TurnEventEmitter` 隔离，不中断模型流。

`MODEL_CALL_COMPLETED`：

```json
{
  "round_index": 2,
  "finish_reason": "stop",
  "output_mode": "final_response",
  "input_tokens": 1420,
  "output_tokens": 233,
  "tool_call_count": 0,
  "duration_ms": 1580
}
```

## 11.3 超时与流关闭

```python
async with asyncio.timeout(timeout_seconds):
    stream = self._model.stream(request)
    try:
        async for event in stream:
            self._raise_if_cancelled(ctx)
            assembler.accept(event)
            await self._emit_model_event_if_needed(ctx, event, assembler)
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()
```

要求：

- `CancelledError` 原样抛出。
- `TimeoutError` 映射为 `ModelInvocationTimeoutError`。
- 流在有公开文本增量之后失败时不得透明重试，否则用户可能收到重复文本。
- 供应商重试应放在 Model Adapter 或 Resilience Decorator 中，且只能在尚未产生语义增量时安全执行。

## 11.4 结束原因校验

| 实际内容 | Finish reason | 处理 |
|---|---|---|
| 最终文本 | `STOP` | 正常 |
| 工具调用 | `TOOL_CALLS` | 正常 |
| 最终文本 | `LENGTH` | 抛 `ModelOutputTruncatedError`，不得把截断文本当成功结果 |
| 任意 | `CONTENT_FILTER` | 抛 `ModelContentFilteredError` |
| 无内容 | 任意 | 抛 `EmptyModelOutputError` |
| 工具调用 | `STOP` | 抛 `InvalidModelFinishReasonError` |
| 文本 | `TOOL_CALLS` | 抛 `InvalidModelFinishReasonError` |

---

## 12. 工具调用准备

## 12.1 JSON 聚合与解析

模型工具参数必须在流结束后一次性解析：

```python
arguments = json.loads(arguments_json)
if not isinstance(arguments, dict):
    raise InvalidToolArgumentsError(...)
```

禁止：

- 使用 `eval()`。
- 宽松执行非 JSON 表达式。
- 自动补全严重残缺的 JSON 后直接执行。
- 把 JSON 解析异常原文返回给用户。

参数 JSON 错误可以生成一个标准工具错误结果回填模型，让模型重新发起正确调用；但该调用仍计入工具调用预算，防止无限修正。

## 12.2 工具名称解析

- 工具名必须精确匹配当前 `available_tools`。
- 不进行模糊匹配、大小写猜测或别名自动映射。
- 未知工具返回 `UNKNOWN_TOOL` 工具错误。
- 不允许模型调用未暴露给本轮的工具，即使全局 Registry 存在同名实现。

## 12.3 Schema 验证

- JSON Schema 验证在 Policy 和 Executor 之前完成。
- 错误消息只包含安全的字段路径和约束摘要。
- 不把完整 Schema 或内部默认值泄漏给模型。
- 可由 Registry 应用显式声明的默认值，但不得猜测缺失业务参数。

## 12.4 调用指纹

规范化算法：

```python
canonical_arguments = json.dumps(
    arguments,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
fingerprint_source = f"{tool_name}\n{canonical_arguments}"
fingerprint = sha256(fingerprint_source.encode("utf-8")).hexdigest()
```

指纹不进入公开事件；公开事件只可包含短前缀用于关联，例如前 12 位。

---

## 13. 工具策略与审批

## 13.1 策略判定顺序

同一模型轮中的所有调用先完成 Policy 评估，再决定是否执行：

```text
prepared calls
    ↓
Policy.evaluate(call 0..n)
    ↓
是否存在 REQUIRE_APPROVAL？
    ├── 是：整个批次不执行，创建 PendingApprovalBatch
    └── 否：DENY 生成合成错误；ALLOW 进入执行器
```

这样可以避免：第一个工具已经产生副作用，第二个工具才发现需要人工审批。

## 13.2 待审批模型

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PendingApprovalItem:
    call: ToolCall
    tool_name: str
    risk_level: ToolRiskLevel
    side_effect: ToolSideEffect
    reason_code: str
    approval_prompt: str


@dataclass(frozen=True, slots=True)
class PendingApprovalBatch:
    approval_id: str
    turn_id: str
    actor_id: str
    session_id: str
    created_at: datetime
    expires_at: datetime | None
    items: tuple[PendingApprovalItem, ...]
```

暂停时：

```python
ctx.pending_approval = pending
ctx.loop_checkpoint = checkpoint
ctx.status = TurnStatus.WAITING_APPROVAL
ctx.output_text = self._config.approval_message
```

随后正常返回，让后续阶段执行：

- `KnowledgeExtractionPhase`：检测 `WAITING_APPROVAL` 后 no-op。
- `PersistencePhase`：保存审批请求、完整工具批次和 Loop Checkpoint。
- `TurnFinalizePhase`：生成 `WAITING_APPROVAL` 的 `TurnResult`。

`AgentLoopPhase` 本身不写数据库。

## 13.3 检查点

```python
@dataclass(frozen=True, slots=True)
class AgentLoopCheckpoint:
    original_turn_id: str
    approval_id: str
    model_messages: tuple[ModelMessage, ...]
    tool_plan: ToolCallPlan
    model_calls_used: int
    tool_rounds_used: int
    total_tool_calls_used: int
    usage: UsageSummary
    integrity_hash: str
```

检查点必须可序列化，不包含：

- Model SDK 对象。
- Exception。
- EventSink。
- 数据库连接。
- CancellationToken。

`integrity_hash` 覆盖 actor、session、审批 ID、调用 ID 和参数指纹，恢复时必须校验。

## 13.4 恢复执行

审批恢复应使用明确的 Channel 无关命令，而不是任意 metadata：

```python
class ApprovalAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ApprovalDecisionCommand:
    approval_id: str
    actions: Mapping[str, ApprovalAction]  # call_id -> action
```

`AgentRequest` 增加：

```python
control: ApprovalDecisionCommand | None = None
```

恢复流程：

1. `StateLoadPhase` 加载审批记录和检查点。
2. `ContextAssemblyPhase` 恢复规范消息序列。
3. `AgentLoopPhase` 校验 actor、session、approval ID、调用 ID 和检查点摘要。
4. 已批准调用使用原始 `idempotency_key` 执行。
5. 已拒绝调用生成 `DENIED_BY_USER` 工具结果。
6. 所有结果按原始顺序回填。
7. AgentLoop 继续下一次模型调用，生成最终自然语言回复。
8. 审批记录由 `PersistencePhase` 标记已消费；同一审批不可重复恢复。

---

## 14. 工具批量执行

## 14.1 执行分类

每个 `ALLOW` 调用按以下规则分类：

### 可并发

同时满足：

- `definition.parallel_safe is True`。
- `definition.side_effect is NONE`。
- Policy 没有要求串行。
- 当前批次中没有声明依赖关系。

### 必须串行

任一条件成立：

- 有本地或外部副作用。
- 非幂等。
- `parallel_safe=False`。
- Policy 要求串行。

最终执行器按原始顺序扫描工具调用，把连续的可并发只读调用组成并发组；副作用调用逐个执行。无论实际完成顺序如何，结果都按 `ordinal` 排序后回填。

## 14.2 并发实现

使用 `asyncio.TaskGroup` 和 Semaphore：

```python
semaphore = asyncio.Semaphore(self._config.max_parallel_tools)
results: dict[int, ToolExecutionResult] = {}

async def run_one(item: PreparedToolCall) -> None:
    async with semaphore:
        results[item.call.ordinal] = await self._execute_one(ctx, item)

async with asyncio.TaskGroup() as group:
    for item in parallel_group:
        group.create_task(run_one(item))
```

规则：

- TaskGroup 内某个工具抛基础设施异常时，其他未完成任务会被取消。
- 可预期工具失败应返回 `ToolExecutionResult`，不触发 TaskGroup 级取消。
- `CancelledError` 必须继续传播。
- 并发组结束后按 `ordinal` 取回结果。

## 14.3 单工具执行

执行步骤：

1. 检查取消。
2. 检查 Turn 剩余时间。
3. 计算 `min(tool timeout, remaining turn time)`。
4. 发送 `TOOL_CALL_STARTED`。
5. 在 `asyncio.timeout()` 中调用 Executor。
6. 验证返回的 `call_id` 和 `tool_name`。
7. 截断过长 `model_content`。
8. 创建 `ToolExecutionRecord`。
9. 发送完成或失败事件。
10. 返回标准结果。

工具级超时默认转成：

```python
ToolExecutionResult(
    status=ToolExecutionStatus.TIMED_OUT,
    model_content=(
        '{"error":{"code":"TOOL_TIMEOUT",'
        '"message":"工具执行超时，未取得结果。"}}'
    ),
    safe_message="工具执行超时",
    error_code="TOOL_TIMEOUT",
    retryable=True,
)
```

若触发的是整个 Turn deadline，则抛 `TurnDeadlineExceededError`，不再回填普通工具结果。

## 14.4 Policy Deny 合成结果

`DENY` 不调用 Executor，直接生成：

```python
ToolExecutionResult(
    call_id=call.call_id,
    tool_name=call.tool_name,
    status=ToolExecutionStatus.DENIED,
    model_content=json.dumps(
        {
            "error": {
                "code": decision.reason_code,
                "message": decision.safe_message,
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ),
    safe_message=decision.safe_message,
    error_code=decision.reason_code,
    retryable=False,
)
```

模型收到该结果后可以生成不执行该操作的解释，但若再次请求相同调用，会被循环保护拦截。

## 14.5 工具结果回填顺序

规范消息顺序必须是：

```text
AssistantMessage(tool_calls=[call-A, call-B])
ToolMessage(call-A result)
ToolMessage(call-B result)
```

即使 B 比 A 先执行完成，也不能改变回填顺序。

---

## 15. 循环与预算保护

## 15.1 硬限制

以下条件立即终止 Agent Loop：

- 工具轮数超过 `max_tool_rounds`。
- 工具总调用数超过 `max_total_tool_calls`。
- 单轮工具调用数超过 `max_tool_calls_per_round`。
- 模型调用次数超过 `max_tool_rounds + 1`。
- 最终文本超过 `max_model_text_chars`。

对应稳定错误：

```text
MaxToolRoundsExceededError
MaxTotalToolCallsExceededError
MaxToolCallsPerRoundExceededError
MaxModelCallsExceededError
ModelOutputTooLargeError
```

## 15.2 重复指纹检测

`ToolLoopGuard` 保存最近调用历史：

```python
@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    fingerprint: str
    result_code: str | None
    result_digest: str
```

判定：

- 相同指纹累计出现次数超过 `max_repeated_fingerprint`，抛 `RepeatedToolCallError`。
- 同一指纹在结果摘要没有变化的情况下连续出现，优先判为循环。
- 参数变化会产生不同指纹，不属于完全重复，但仍参与周期检测。

## 15.3 周期检测

在最近 `cycle_detection_window` 个指纹中检测长度 2 到 4 的重复序列，例如：

```text
A, B, A, B
A, B, C, A, B, C
```

发现重复周期且对应结果摘要未发生有效变化，抛 `ToolCallCycleDetectedError`。

循环错误是 Runtime Error，不再额外调用模型“尝试自救”，避免把失控循环继续放大。错误 Mapper 应向用户返回安全信息，例如：

```text
操作未能收敛，已停止重复工具调用。
```

---

## 16. 超时与取消

## 16.1 三层时间限制

```text
Turn Deadline
    ├── Model Call Timeout
    └── Tool Call Timeout
```

有效超时：

```python
effective_timeout = min(configured_timeout, remaining_turn_seconds)
```

若剩余时间小于等于零，直接抛 `TurnDeadlineExceededError`。

## 16.2 检查点

必须在以下位置检查取消和 deadline：

- 进入 AgentLoop。
- 每次模型调用前。
- 模型流的每个事件后。
- 每批工具策略判定前后。
- 每个工具执行前后。
- 每次回填工具结果后。
- 进入下一模型轮前。

## 16.3 取消语义

- `asyncio.CancelledError` 不写成普通 ToolResult。
- 并发工具任务全部取消。
- 不发送误导性的 `TOOL_CALL_FAILED`；可以发送带 `cancelled=true` 的终止事件，但不能吞掉取消。
- Kernel 捕获取消后设置 `CANCELLED` 并执行 Cleanup。
- Persistence 是否保存取消前记录由整体 Runtime 策略决定，AgentLoop 不直接提交。

---

## 17. Usage 与工具记录

## 17.1 Usage 汇总

```python
@dataclass(frozen=True, slots=True)
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
```

更新规则：

- 每完成一次模型调用，`model_calls += 1`。
- Provider 返回的每轮输入、输出 token 分别累加。
- `total_tokens` 始终重新计算为输入与输出之和，不信任不一致的供应商总数。
- 每个被模型正式接受的工具调用都计入 `tool_calls`，包括参数错误、Policy Deny 和超时；待审批但尚未执行的调用不计入已执行工具数，可另存 `pending_tool_calls`。

建议扩充 `ToolExecutionRecord`：

```python
@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    call_id: str
    tool_name: str
    status: ToolExecutionStatus
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_code: str | None = None
    retryable: bool = False
    idempotency_key: str | None = None
    arguments_fingerprint: str | None = None
```

不保存完整工具参数和结果正文到公共记录；需要审计时由 PersistencePhase 写入受控审计存储。

---

## 18. 事件定义

原框架中的事件类型保留，并增加审批与挂起事件：

```python
class AgentEventType(StrEnum):
    # 原有事件略
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_DELTA = "model_delta"
    MODEL_CALL_COMPLETED = "model_call_completed"

    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"

    TURN_SUSPENDED = "turn_suspended"
```

## 18.1 工具事件安全字段

`TOOL_CALL_STARTED`：

```json
{
  "round_index": 1,
  "call_id": "call-123",
  "tool_name": "calendar.create_event",
  "ordinal": 0,
  "arguments_fingerprint": "8f2a4d70c1b9"
}
```

不得包含完整参数。

`TOOL_CALL_COMPLETED`：

```json
{
  "call_id": "call-123",
  "tool_name": "calendar.create_event",
  "status": "succeeded",
  "duration_ms": 420,
  "artifact_count": 0
}
```

不得包含完整结果正文。

`TOOL_CALL_FAILED`：

```json
{
  "call_id": "call-123",
  "tool_name": "calendar.create_event",
  "status": "failed",
  "error_code": "CALENDAR_UNAVAILABLE",
  "retryable": true,
  "duration_ms": 800
}
```

不得包含 Exception、堆栈、URL 凭据或内部主机名。

## 18.2 审批事件

```json
{
  "approval_id": "approval-123",
  "tool_count": 2,
  "expires_at": "2026-06-25T10:00:00Z",
  "items": [
    {
      "call_id": "call-1",
      "tool_name": "email.send",
      "risk_level": "high",
      "approval_prompt": "确认发送该邮件？"
    }
  ]
}
```

审批展示所需的参数摘要应由 Policy 层生成，不能由通用事件层直接暴露原始参数。

---

## 19. 错误体系

新增或正式实现以下错误：

```text
InvalidAgentLoopStateError
ModelInvocationError
ModelInvocationTimeoutError
ModelStreamProtocolError
MixedModelOutputError
EmptyModelOutputError
InvalidModelFinishReasonError
ModelOutputTruncatedError
ModelOutputTooLargeError
ModelContentFilteredError
ContextWindowExceededError
UnknownToolError
InvalidToolArgumentsError
DuplicateToolCallIdError
ToolExecutionError
ToolResultProtocolError
ToolCallTimeoutError
MaxToolRoundsExceededError
MaxTotalToolCallsExceededError
MaxToolCallsPerRoundExceededError
MaxModelCallsExceededError
RepeatedToolCallError
ToolCallCycleDetectedError
TurnDeadlineExceededError
InvalidApprovalCheckpointError
ApprovalExpiredError
ApprovalAlreadyConsumedError
```

错误分层：

### 可回填模型的工具错误

- 未知工具。
- 参数 JSON 错误。
- Schema 校验错误。
- Policy Deny。
- 普通工具业务失败。
- 单工具超时且 Turn 仍有时间。

这些错误转成 `ToolExecutionResult`，让模型生成自然语言响应。

### 必须终止 Turn 的 Runtime Error

- 模型流协议错误。
- 混合模型输出。
- 上下文窗口无法适配。
- 工具循环。
- 预算耗尽。
- Turn deadline。
- 工具执行框架返回不一致结果。
- 检查点篡改或审批恢复无效。

### 必须原样传播

- `asyncio.CancelledError`。
- `KeyboardInterrupt`、`SystemExit` 等 `BaseException`，不得用 `except Exception` 之外的宽泛捕获吞掉。

---

## 20. 安全要求

1. 不向事件发送系统 Prompt、隐藏模型消息或完整上下文。
2. 不发送完整工具参数；只发送工具名、call ID、参数指纹和经过 Policy 生成的审批摘要。
3. 不把工具异常堆栈回填模型。
4. Tool Adapter 必须清理令牌、Cookie、Authorization Header 和内部网络地址。
5. 工具结果中包含不受信任文本时，应明确标记为工具输出，不能作为新的系统指令解释。
6. AgentLoop 不执行工具结果中的代码、模板或嵌套工具调用。
7. 模型只能调用本轮公开的 `available_tools`。
8. 外部副作用工具必须具有 Policy 判定和幂等语义。
9. 审批恢复必须绑定 actor、session、approval ID 和原始参数摘要。
10. 审批一旦消费，不允许重复执行同一工具批次。
11. 不暴露模型隐藏推理过程。
12. 工具输出长度受限，大内容转 Artifact。

---

## 21. 可观测性

建议 Trace 层记录以下 Span：

```text
agent_loop
├── model_call[0]
├── tool_policy[0]
├── tool_call[call-a]
├── tool_call[call-b]
├── model_call[1]
└── ...
```

Span 属性只记录低敏感字段：

```text
turn_id
request_id
round_index
tool_name
call_id
finish_reason
status
duration_ms
input_tokens
output_tokens
error_code
retryable
```

禁止记录：

```text
system_prompt
full_messages
raw_tool_arguments
raw_tool_result
access_token
api_key
exception_stack_in_public_event
```

日志与 Trace 可以记录内部异常堆栈，但必须进入受控日志系统，不能复制到 AgentEvent。

---

## 22. `AgentLoopPhase` 代码骨架

以下骨架展示模块边界和异常控制，不省略关键运行语义：

```python
from __future__ import annotations

import asyncio
from contextlib import aclosing

from cogito_agent.runtime.context import TurnContext
from cogito_agent.runtime.phase import BasePhase


class AgentLoopPhase(BasePhase):
    name = "agent_loop"

    def __init__(
        self,
        *,
        model: ModelPort,
        context_window: ModelContextWindowPort,
        tool_registry: ToolRegistryPort,
        tool_policy: ToolPolicyPort,
        tool_executor: ToolExecutorPort,
        clock: ClockPort,
        config: AgentLoopConfig,
        assembler_factory: ModelResponseAssemblerFactory,
        loop_guard_factory: ToolLoopGuardFactory,
    ) -> None:
        self._model = model
        self._context_window = context_window
        self._tool_registry = tool_registry
        self._tool_policy = tool_policy
        self._tool_executor = tool_executor
        self._clock = clock
        self._config = config
        self._assembler_factory = assembler_factory
        self._loop_guard_factory = loop_guard_factory

    async def execute(self, ctx: TurnContext) -> None:
        self._validate_entry_state(ctx)
        loop_guard = self._loop_guard_factory.create(self._config)

        if ctx.loop_checkpoint is not None:
            await self._resume_checkpoint(ctx, loop_guard)
            if ctx.status is TurnStatus.WAITING_APPROVAL:
                return

        while ctx.output_text is None:
            self._check_cancellation_and_deadline(ctx)
            self._check_model_budget(ctx)

            request = await self._build_model_request(ctx)
            output = await self._invoke_model(ctx, request)

            ctx.model_calls_used += 1
            ctx.model_responses.append(output)
            self._add_usage(ctx, output)

            if output.text is not None:
                self._accept_final_response(ctx, output)
                return

            self._check_tool_budget(ctx, output.tool_calls)
            plan = self._prepare_tool_plan(ctx, output.tool_calls)
            loop_guard.check_batch(plan)

            ctx.model_messages.append(
                AssistantMessage(
                    tool_calls=plan.original_calls,
                    provider_response_id=output.provider_response_id,
                )
            )

            decisions = await self._evaluate_policy(
                ctx,
                plan.executable_calls,
            )
            if any(
                item.decision is ToolPolicyDecisionType.REQUIRE_APPROVAL
                for item in decisions
            ):
                self._create_approval_checkpoint(ctx, plan, decisions)
                return

            results = await self._execute_plan(ctx, plan, decisions)
            self._append_results(ctx, plan.original_calls, results)

            ctx.tool_rounds_used += 1
            ctx.total_tool_calls_used += len(plan.original_calls)
            loop_guard.record_batch(plan, results)

        raise InvalidAgentLoopStateError(
            "Agent loop exited without an explicit terminal condition"
        )

    async def _invoke_model(
        self,
        ctx: TurnContext,
        request: ModelInvocationRequest,
    ) -> ModelRoundOutput:
        assembler = self._assembler_factory.create(
            max_text_chars=self._config.max_model_text_chars
        )
        started_at = self._clock.now()

        await self._emit(
            ctx,
            AgentEventType.MODEL_CALL_STARTED,
            {
                "round_index": request.round_index,
                "message_count": len(request.messages),
                "tool_count": len(request.tools),
            },
        )

        try:
            async with asyncio.timeout(request.timeout_seconds):
                async with aclosing(self._model.stream(request)) as stream:
                    async for event in stream:
                        self._raise_if_cancelled(ctx)
                        assembler.accept(event)

                        if assembler.is_public_text_delta(event):
                            await self._emit(
                                ctx,
                                AgentEventType.MODEL_DELTA,
                                assembler.public_delta_payload(event),
                            )

        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ModelInvocationTimeoutError(
                "Model invocation timed out",
                safe_message="模型响应超时",
            ) from exc
        except RuntimeAgentError:
            raise
        except Exception as exc:
            raise ModelInvocationError(
                "Model invocation failed",
                safe_message="模型调用失败",
            ) from exc

        output = assembler.build(round_index=request.round_index)
        duration_ms = self._duration_ms(started_at, self._clock.now())

        await self._emit(
            ctx,
            AgentEventType.MODEL_CALL_COMPLETED,
            {
                "round_index": output.round_index,
                "finish_reason": output.finish_reason,
                "output_mode": (
                    "final_response" if output.text is not None else "tool_calls"
                ),
                "input_tokens": output.input_tokens,
                "output_tokens": output.output_tokens,
                "tool_call_count": len(output.tool_calls),
                "duration_ms": duration_ms,
            },
        )

        return output
```

实现时所有私有方法必须有独立单元测试，尤其是：

- `_prepare_tool_plan()`。
- `_check_tool_budget()`。
- `_create_approval_checkpoint()`。
- `_append_results()`。
- `_remaining_call_timeout()`。

---

## 23. Kernel 与相邻 Phase 的配套调整

## 23.1 Kernel 状态完成规则

原始 Kernel 示例在 Pipeline 执行完成后无条件设置 `COMPLETED`。为了支持待审批和策略拒绝，最终实现应改为：

```python
if ctx.result is None:
    raise MissingTurnResultError()

ctx.status = ctx.result.status

if ctx.status is TurnStatus.COMPLETED:
    await emit_safely(events.turn_completed(ctx))
elif ctx.status is TurnStatus.WAITING_APPROVAL:
    await emit_safely(events.turn_suspended(ctx))
elif ctx.status is TurnStatus.DENIED:
    await emit_safely(events.turn_completed(ctx))
else:
    raise InvalidTerminalTurnStatusError(...)
```

Kernel 仍然不根据 Phase 名称分支，只根据最终结果状态发送生命周期事件。

## 23.2 `KnowledgeExtractionPhase`

```python
if ctx.status is not TurnStatus.RUNNING:
    return

if ctx.final_response is None:
    return
```

待审批状态不进行知识抽取，避免把“等待确认”误当成最终 Agent 结论。

## 23.3 `PersistencePhase`

需要支持两类原子提交：

### 完成路径

- 用户消息。
- 最终 Agent 回复。
- 工具执行记录。
- Usage。
- Session 更新。

### 待审批路径

- 用户消息。
- Assistant 工具调用消息。
- PendingApprovalBatch。
- AgentLoopCheckpoint。
- 当前 Usage 和预算计数。

## 23.4 `TurnFinalizePhase`

```python
if ctx.status is TurnStatus.WAITING_APPROVAL:
    result_status = TurnStatus.WAITING_APPROVAL
    text = ctx.output_text or "该操作需要确认后才能继续。"
else:
    if ctx.output_text is None:
        raise MissingFinalResponseError()
    result_status = TurnStatus.COMPLETED
    text = ctx.output_text
```

---

## 24. Composition Root

```python
def build_agent_loop_phase(
    *,
    model: ModelPort,
    context_window: ModelContextWindowPort,
    tool_registry: ToolRegistryPort,
    tool_policy: ToolPolicyPort,
    tool_executor: ToolExecutorPort,
    clock: ClockPort,
    config: AgentLoopConfig,
) -> AgentLoopPhase:
    return AgentLoopPhase(
        model=model,
        context_window=context_window,
        tool_registry=tool_registry,
        tool_policy=tool_policy,
        tool_executor=tool_executor,
        clock=clock,
        config=config,
        assembler_factory=DefaultModelResponseAssemblerFactory(),
        loop_guard_factory=DefaultToolLoopGuardFactory(),
    )
```

完整 Pipeline：

```python
phases: list[RuntimePhase] = [
    TurnInitPhase(...),
    StateLoadPhase(...),
    InformationRetrievalPhase(...),
    ContextAssemblyPhase(...),
    build_agent_loop_phase(...),
    KnowledgeExtractionPhase(...),
    PersistencePhase(...),
    TurnFinalizePhase(),
]
```

AgentLoop 所有依赖都由 Composition Root 注入；Phase 内不创建 Adapter。

---

## 25. 测试规格

## 25.1 最终文本路径

验证：

- 一次模型调用返回多个文本增量。
- `MODEL_DELTA` 顺序与流一致。
- 最终文本正确拼接。
- 追加一个 `AssistantMessage(content=...)`。
- `ctx.output_text`、`ctx.final_response`、Usage 正确。
- 不调用 Tool Policy 和 Executor。

## 25.2 单工具路径

模型序列：

```text
call 0 → tool call
executor → success
call 1 → final text
```

验证：

- 消息顺序为 Assistant(tool call) → Tool(result) → Assistant(final)。
- 两次模型调用、一个工具轮、一个工具调用。
- 工具事件顺序正确。
- 幂等键稳定。

## 25.3 多工具并发路径

- 三个只读、`parallel_safe=True` 工具。
- 用 Barrier 控制完成顺序为 3、1、2。
- 验证 ToolMessage 回填仍为 1、2、3。
- 验证最大并发不超过配置。

## 25.4 副作用串行路径

- 混合只读和外部写操作。
- 验证副作用工具不进入并发组。
- 验证 Policy 在任何真实执行前全部完成。

## 25.5 流协议

分别测试：

- 文本后出现工具调用 → `MixedModelOutputError`。
- 工具调用后出现文本 → `MixedModelOutputError`。
- 两次 Completed → `ModelStreamProtocolError`。
- Completed 后继续输出 → `ModelStreamProtocolError`。
- 无语义输出 → `EmptyModelOutputError`。
- 工具参数分多个 Delta 正确聚合。
- 多工具 interleaving Delta 正确按 ordinal 聚合。
- `LENGTH` 不接受为成功回答。

## 25.6 工具参数

- 非法 JSON。
- 根节点不是对象。
- 未知工具。
- 缺少必填字段。
- 类型错误。
- 多余字段按 Schema 规则处理。
- call ID 重复。
- 规范化 JSON 产生稳定指纹。

## 25.7 Policy

- 全部 ALLOW。
- 一个 DENY、一个 ALLOW：Denied 生成合成结果，Allowed 正常执行。
- 任一 REQUIRE_APPROVAL：整个批次无真实执行。
- Policy 抛异常：映射为稳定 Runtime Error，不默认放行。

## 25.8 审批

- PendingApprovalBatch 字段完整。
- Checkpoint 不含不可序列化对象。
- 待审批状态进入 Persistence 和 Finalize。
- 审批通过后恢复执行。
- 用户拒绝后生成标准 ToolMessage 并继续模型调用。
- actor 不匹配。
- session 不匹配。
- approval ID 不匹配。
- checkpoint hash 不匹配。
- approval 已消费。
- approval 已过期。

## 25.9 限制与循环

- 达到最大工具轮数。
- 超过工具总数。
- 单轮工具过多。
- 超过最大模型调用数。
- 相同调用重复超过阈值。
- A-B-A-B 周期。
- 参数变化时不误判完全重复。
- 相同调用但工具结果发生有效变化时按策略处理。

## 25.10 超时

- 模型调用超时。
- 工具调用超时但 Turn 仍有剩余时间：回填 timeout ToolResult。
- Turn deadline 到期：终止 Turn。
- 配置工具超时大于剩余时间：使用剩余时间。
- 模型流关闭函数被调用。

## 25.11 取消

在以下时点取消 Task：

- 模型调用前。
- 模型流中。
- Policy 评估中。
- 单工具执行中。
- 并发工具组中。
- 工具完成、结果回填前。

验证：

- `CancelledError` 未包装。
- 子任务取消。
- AgentLoop 不设置错误的最终回复。
- Kernel Cleanup 执行。

## 25.12 EventSink 故障

- 每种模型和工具事件均让 Sink 抛异常。
- 验证 AgentLoop 正常完成。
- 内部日志记录故障。
- 不重复执行模型或工具。

## 25.13 Usage

- 多模型轮累加。
- 输入和输出 token 分开累加。
- `total_tokens` 恒等于两者之和。
- Policy Deny 是否计入工具调用符合定义。
- 待审批调用不计入已执行工具数。

## 25.14 上下文窗口

- 每次模型调用都会调用 `ModelContextWindowPort.fit()`。
- 工具结果增加后再次适配。
- Canonical `ctx.model_messages` 不被 fit() 修改。
- 无法适配时抛稳定错误。

## 25.15 架构测试

确保以下目录不得导入：

```text
openai
anthropic
redis
nats
kafka
rabbitmq
sqlalchemy
fastapi
starlette
telegram
discord
cogito_agent.application.messaging
```

确保：

- `ToolExecutorPort` 不接收 `TurnContext`。
- `AgentLoopPhase` 不导入 Repository 或 UnitOfWork。
- `AgentLoopPhase` 不调用 `asyncio.run()`。
- 不使用裸 `except:`。
- 不捕获 `BaseException`。

---

## 26. 实现提交顺序

以下顺序用于降低交叉修改，但每一步都按照本文档中的最终接口实现，不建立临时 API：

1. 定义 `domain/model.py`、`domain/tools.py`、`domain/messages.py` 和 `domain/approval.py`。
2. 定义 `ModelPort`、`ModelContextWindowPort`、`ToolRegistryPort`、`ToolPolicyPort`、`ToolExecutorPort`。
3. 扩展 `TurnContext`、`UsageSummary`、`ToolExecutionRecord` 和 Event Type。
4. 实现 `ModelResponseAssembler` 及其协议测试。
5. 实现 `ToolLoopGuard` 及重复、周期测试。
6. 实现参数准备、Schema 校验和工具指纹逻辑。
7. 实现 Policy 批量判定与 PendingApproval/Checkpoint 构建。
8. 实现单工具执行、结果校验、超时和安全事件。
9. 实现受限并发 `ToolBatchExecutor` 和确定性结果排序。
10. 实现 `AgentLoopPhase.execute()` 主循环。
11. 调整 Kernel、KnowledgeExtraction、Persistence、TurnFinalize 的待审批状态语义。
12. 在 Composition Root 注入全部依赖。
13. 完成成功、失败、取消、审批恢复和架构边界测试。
14. 运行类型检查、lint、单元测试和架构测试。

任何提交都不应引入：

- `object` 形式的模型响应或工具调用。
- 假模型回复。
- 假工具结果。
- Provider SDK 对象泄漏。
- 未经测试的审批旁路。
- 用 metadata 代替正式状态字段。

---

## 27. 验收标准

### 27.1 功能

- [ ] 模型可直接返回最终文本。
- [ ] 模型可发起单个或多个工具调用。
- [ ] 工具结果按规范消息顺序回填。
- [ ] 模型在工具结果后可继续调用工具或给出最终回复。
- [ ] 最终回复支持安全实时增量事件。
- [ ] 工具调用流支持分片参数聚合。
- [ ] 支持 Policy Allow、Deny 和 Require Approval。
- [ ] 支持审批检查点保存与恢复。
- [ ] 支持安全的只读工具受限并发。
- [ ] 支持 Turn、模型和工具三级超时。
- [ ] 支持 Task 取消。
- [ ] 支持重复调用和周期循环检测。
- [ ] 支持每轮动态上下文窗口适配。
- [ ] Usage 和工具记录完整准确。

### 27.2 边界

- [ ] AgentLoop 不导入 Channel、MessageBus、Repository 或数据库实现。
- [ ] AgentLoop 不知道模型供应商。
- [ ] Tool Executor 不接收 TurnContext。
- [ ] AgentLoop 不直接持久化。
- [ ] AgentLoop 不提取用户偏好或长期记忆。
- [ ] EventSink 故障不破坏 Turn。
- [ ] 未实现能力不返回伪成功。

### 27.3 安全

- [ ] 事件不包含完整系统 Prompt。
- [ ] 事件不包含完整工具参数和结果。
- [ ] 工具异常堆栈不回填模型或 Channel。
- [ ] 模型不能调用未公开工具。
- [ ] 外部副作用工具具备策略和幂等控制。
- [ ] 审批恢复绑定 actor、session 和完整性摘要。
- [ ] 不暴露隐藏思维链。

### 27.4 质量

- [ ] Python 3.12+。
- [ ] 全部公共接口有类型注解。
- [ ] 核心 DTO 使用 `dataclass(frozen=True, slots=True)`。
- [ ] `TurnContext` 使用正式强类型字段。
- [ ] `pytest`、`pytest-asyncio` 全部通过。
- [ ] mypy 或 pyright 严格检查通过。
- [ ] Ruff 或等价 lint 通过。
- [ ] 架构依赖测试通过。
- [ ] 取消、超时和 EventSink 故障均有测试。

---

## 28. 最终运行序列示例

```text
AgentLoopPhase.execute
│
├── fit model context
├── emit MODEL_CALL_STARTED(round=0)
├── model.stream
│   └── tool call: search_documents({"query":"..."})
├── emit MODEL_CALL_COMPLETED(mode=tool_calls)
├── validate tool + args + budget + loop guard
├── policy: ALLOW
├── append AssistantMessage(tool_calls)
├── emit TOOL_CALL_STARTED
├── execute tool
├── emit TOOL_CALL_COMPLETED
├── append ToolMessage(result)
│
├── fit model context
├── emit MODEL_CALL_STARTED(round=1)
├── model.stream
│   ├── MODEL_DELTA("根据检索结果，")
│   ├── MODEL_DELTA("...")
│   └── completed(stop)
├── emit MODEL_CALL_COMPLETED(mode=final_response)
├── append AssistantMessage(content)
├── set ctx.final_response
├── set ctx.output_text
└── return
```

在此边界下，`AgentLoopPhase` 完成推理与行动闭环；后续 `KnowledgeExtractionPhase` 只读取最终用户输入和 Agent 输出，`PersistencePhase` 统一持久化，`TurnFinalizePhase` 构建结果。Kernel、MessageBus 和 Channel 均无需知道模型或工具循环的内部细节。
