# Cogito-Agent 初始框架实现规格

> 本文档用于交给实现型 AI，生成 Cogito-Agent 的第一版运行时框架。
>
> 目标是先建立稳定、可扩充、与 Channel / MessageBus 解耦的 Agent Kernel 骨架。  
> 本阶段只实现框架、类型、接口、阶段编排、事件机制和基础测试，不实现各 Phase 的具体业务逻辑。

---

## 1. 实现任务

请按照本文档实现一个 Python 异步 Agent Runtime 初始框架。

本次实现范围：

1. 建立 Channel 无关、MessageBus 无关的 `RuntimeKernel`。
2. 建立固定顺序、可扩充的 Phase Pipeline。
3. 建立 8 个职责单一的 Phase 空壳。
4. 建立强类型 `TurnContext`。
5. 建立 Channel 无关的 `AgentRequest`、`TurnResult` 和 `AgentEvent`。
6. 建立 Port 接口，不实现真实数据库、向量库、模型、工具或 MessageBus Adapter。
7. 建立 Application 层的 `AgentApplicationService`。
8. 为以后连接 MessageBus 预留 Worker、Mapper 和 Publisher Port。
9. 建立单元测试，验证框架行为、阶段顺序、异常处理和依赖边界。

本次明确不实现：

- LLM 的真实调用。
- 工具的真实调用。
- 关键词检索算法。
- 向量检索算法。
- Rerank 算法。
- 用户偏好抽取逻辑。
- 数据库表结构与真实持久化。
- Redis、NATS、RabbitMQ、Kafka 等 MessageBus Adapter。
- Telegram、Discord、Web、CLI 等 Channel Adapter。
- 插件热加载。
- 拓扑排序。
- 并行 Phase 调度。
- 多租户、高并发和分布式执行。

---

## 2. 核心设计原则

### 2.1 Kernel 不感知 Channel

`RuntimeKernel` 不允许导入或引用以下类型：

- Telegram message
- Discord message
- HTTP request
- WebSocket connection
- CLI command
- Channel router
- Channel name-specific DTO

Channel Adapter 将平台消息转换为统一的 MessageBus 消息；Application Worker 再将 MessageBus 消息转换为 `AgentRequest`。

Kernel 只接受 `AgentRequest`。

### 2.2 Kernel 不感知 MessageBus

`RuntimeKernel` 不允许直接依赖：

- Redis
- NATS
- RabbitMQ
- Kafka
- MessageEnvelope
- Topic
- Queue
- Ack / Nack
- Consumer
- Producer

Kernel 只产生：

- `AgentEvent`
- `TurnResult`
- 标准化 Runtime Error

MessageBus 发布由 Kernel 外部的 Application Worker 负责。

### 2.3 固定顺序，不做拓扑排序

Phase 的执行顺序由 Composition Root 中的列表明确决定：

```python
phases = [
    TurnInitPhase(...),
    StateLoadPhase(...),
    InformationRetrievalPhase(...),
    ContextAssemblyPhase(...),
    AgentLoopPhase(...),
    KnowledgeExtractionPhase(...),
    PersistencePhase(...),
    TurnFinalizePhase(...),
]
```

禁止：

- 根据字符串依赖进行拓扑排序。
- 通过 `requires` / `produces` 自动推导顺序。
- 运行时动态改变 Phase 顺序。
- 隐式扫描模块并自动注册。

后续扩充 Phase 时，只修改 Composition Root 的组装列表，不修改 Kernel 主循环。

### 2.4 每个 Phase 职责单一

Phase 应当围绕一个清晰的业务边界建立。

一个逻辑适合成为独立 Phase，通常应至少满足以下一项：

- 有独立输入或输出语义。
- 有独立失败语义。
- 需要独立观测耗时。
- 未来可能独立关闭。
- 未来可能独立重试。
- 涉及外部系统或事务边界。
- 与前后逻辑有明确的数据边界。

同一职责的不同策略，应作为 Phase 内部组件，而不是无限拆分顶层 Phase。

例如：

```text
InformationRetrievalPhase
    ├── KeywordRetriever
    ├── VectorRetriever
    ├── PreferenceRetriever
    ├── HistoryRetriever
    ├── ResultFusion
    └── Reranker
```

这些组件共同完成“检索相关信息”，因此属于同一个 Phase。

### 2.5 核心数据强类型化

不要把全部运行状态放入：

```python
dict[str, Any]
```

核心运行数据必须定义在 `TurnContext` 中。

只允许保留一个有限的扩展字段：

```python
metadata: dict[str, object]
```

它仅用于临时扩展，不得替代正式字段。

### 2.6 异步优先

所有涉及 I/O 的接口使用 `async`：

- 模型调用。
- 数据库访问。
- 向量检索。
- 关键词检索。
- 工具执行。
- 事件输出。
- MessageBus 发布。

本项目不追求高并发，但 LLM、检索、数据库和工具调用均为 I/O，异步仍然是正确的基础模型。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────┐
│ Channel Adapters                             │
│ Telegram / Discord / Web / CLI               │
└──────────────────────┬───────────────────────┘
                       │ Channel-specific input
                       ▼
┌──────────────────────────────────────────────┐
│ MessageBus                                   │
│ 只负责消息传递、路由、重试、ack、correlation  │
└──────────────────────┬───────────────────────┘
                       │ MessageEnvelope
                       ▼
┌──────────────────────────────────────────────┐
│ AgentMessageWorker                           │
│                                              │
│ MessageEnvelope → AgentRequest               │
│ 调用 AgentApplicationService / RuntimeKernel │
│ AgentEvent / TurnResult → MessageEnvelope    │
└──────────────────────┬───────────────────────┘
                       │ AgentRequest
                       ▼
┌──────────────────────────────────────────────┐
│ RuntimeKernel                                │
│                                              │
│ 1. TurnInit                                  │
│ 2. StateLoad                                 │
│ 3. InformationRetrieval                      │
│ 4. ContextAssembly                           │
│ 5. AgentLoop                                 │
│ 6. KnowledgeExtraction                       │
│ 7. Persistence                               │
│ 8. TurnFinalize                              │
└──────────────────────┬───────────────────────┘
                       │ Ports
                       ▼
┌──────────────────────────────────────────────┐
│ Model / Tool / Memory / Storage Adapters     │
└──────────────────────────────────────────────┘
```

### 3.1 允许的依赖方向

```text
Channel Adapter → MessageBus Port
AgentMessageWorker → MessageBus Port
AgentMessageWorker → AgentApplicationService
AgentApplicationService → RuntimeKernel
RuntimeKernel → RuntimePhase
RuntimePhase → Domain Port
Infrastructure Adapter → Domain Port
```

### 3.2 禁止的依赖方向

```text
RuntimeKernel → MessageBus implementation
RuntimeKernel → Channel Adapter
RuntimePhase → Telegram / Discord / Web
Domain Model → Infrastructure Adapter
Domain Port → Redis / SQLAlchemy / NATS concrete type
```

---

## 4. 八阶段 Pipeline

```text
AgentRequest
    │
    ▼
1. TurnInit
    │
    ▼
2. StateLoad
    │
    ▼
3. InformationRetrieval
    │
    ▼
4. ContextAssembly
    │
    ▼
5. AgentLoop
    │
    ▼
6. KnowledgeExtraction
    │
    ▼
7. Persistence
    │
    ▼
8. TurnFinalize
    │
    ▼
TurnResult
```

### 4.1 TurnInitPhase

职责：

- 校验请求的基础字段。
- 创建 `turn_id`。
- 记录开始时间。
- 初始化 Trace 上下文。
- 初始化运行状态。
- 设置本轮最大工具轮数、超时等运行参数。

禁止：

- 加载数据库状态。
- 执行检索。
- 构建 Prompt。
- 调用模型。
- 发布 MessageBus 消息。

### 4.2 StateLoadPhase

职责：

- 加载 Session。
- 加载最近历史消息。
- 加载 Session Summary。
- 加载用户基础档案。
- 加载确定性的用户设置。
- 加载会话级配置。

这里处理的是确定性状态加载，而不是相关性检索。

禁止：

- 向量相似度检索。
- 关键词相关性检索。
- 组装模型上下文。
- 调用模型。
- 写入数据库。

### 4.3 InformationRetrievalPhase

职责：

- 根据当前用户输入建立检索请求。
- 执行关键词检索。
- 执行向量检索。
- 检索用户偏好。
- 检索相关历史事件。
- 检索长期记忆。
- 执行去重、融合、排序和权限过滤。
- 将检索结果写入 `TurnContext.retrieved_items`。

内部组件可包括：

```text
KeywordRetriever
VectorRetriever
PreferenceRetriever
HistoryRetriever
LongTermMemoryRetriever
RetrievalFusion
RetrievalReranker
```

禁止：

- 直接构建最终模型 Messages。
- 调用 LLM 生成最终回答。
- 写入数据库。
- 提取新的用户偏好。

### 4.4 ContextAssemblyPhase

职责：

- 合并当前输入、近期历史、摘要、用户偏好和检索结果。
- 执行上下文去重。
- 执行 Token Budget 分配。
- 构建 System / User / Assistant / Tool Message。
- 加载或生成可见 Tool Schema。
- 形成 `TurnContext.model_messages`。
- 形成 `TurnContext.available_tools`。

禁止：

- 自己执行关键词或向量检索。
- 调用最终回答模型。
- 执行工具。
- 持久化数据。

### 4.5 AgentLoopPhase

职责：

- 调用模型。
- 接收流式文本增量。
- 解析 Tool Calls。
- 执行工具。
- 将工具结果写回模型上下文。
- 循环执行 Model → Tool → Model。
- 执行最大轮数限制。
- 执行工具循环检测。
- 处理取消和超时。
- 最终形成 `TurnContext.final_response`。

基本流程：

```text
ModelCall
   ↓
是否存在 ToolCall？
   ├─ 否 → 保存最终回答并结束
   └─ 是
       ↓
     ToolExecute
       ↓
     ToolResult 写入 model_messages
       ↓
     再次 ModelCall
```

禁止：

- 提取长期偏好。
- 更新用户偏好数据库。
- 保存长期记忆。
- 直接向 MessageBus 发布。

### 4.6 KnowledgeExtractionPhase

职责：

- 从本轮用户输入和 Agent 最终输出中提取知识候选。
- 识别新的用户偏好。
- 识别用户事实。
- 识别长期目标。
- 识别记忆候选。
- 识别摘要更新候选。
- 识别偏好的新增、修改和删除意图。
- 给候选项附加来源和置信度。

输出应是候选对象，而不是直接写数据库：

```text
PreferenceCandidate
MemoryCandidate
SummaryCandidate
UserFactCandidate
```

禁止：

- 直接写数据库。
- 将低置信度推断直接视为事实。
- 在此阶段决定事务提交。

### 4.7 PersistencePhase

职责：

- 保存本轮用户消息。
- 保存本轮 Agent 回复。
- 保存工具执行记录。
- 更新 Session。
- 更新 Session Summary。
- 应用用户偏好候选。
- 保存长期记忆候选。
- 保存 Embedding。
- 使用 Unit of Work 或等价事务边界提交。

偏好写入规则由后续具体实现决定，但框架应允许：

- insert
- update
- delete
- ignore
- tentative

禁止：

- 再次调用模型生成回答。
- 再次执行相关性检索。
- 向 MessageBus 发布最终响应。

### 4.8 TurnFinalizePhase

职责：

- 根据 `TurnContext` 创建 `TurnResult`。
- 汇总 Usage。
- 汇总 Tool Records。
- 标记本轮状态。
- 结束正常路径的 Trace 信息。

必须注意：

- 无论在哪个 Phase 失败，底层资源清理都必须由 Kernel 的 `finally` 保证。
- `TurnFinalizePhase` 负责正常完成结果的构建。
- Kernel 的 `finally` 负责无条件清理。

禁止：

- 执行业务检索。
- 调用模型。
- 写 MessageBus。
- 隐式吞掉错误。

---

## 5. 推荐目录结构

请优先使用以下目录结构。可以根据现有仓库命名做小幅调整，但不得破坏层次边界。

```text
cogito_agent/
├── application/
│   ├── __init__.py
│   ├── agent_service.py
│   └── messaging/
│       ├── __init__.py
│       ├── envelope.py
│       ├── mapper.py
│       ├── ports.py
│       └── worker.py
│
├── runtime/
│   ├── __init__.py
│   ├── kernel.py
│   ├── context.py
│   ├── events.py
│   ├── errors.py
│   ├── models.py
│   ├── cleanup.py
│   ├── phase.py
│   │
│   └── phases/
│       ├── __init__.py
│       ├── turn_init.py
│       ├── state_load.py
│       ├── information_retrieval.py
│       ├── context_assembly.py
│       ├── agent_loop.py
│       ├── knowledge_extraction.py
│       ├── persistence.py
│       └── turn_finalize.py
│
├── domain/
│   ├── __init__.py
│   ├── messages.py
│   ├── retrieval.py
│   ├── preferences.py
│   ├── memory.py
│   ├── tools.py
│   └── usage.py
│
├── ports/
│   ├── __init__.py
│   ├── clock.py
│   ├── ids.py
│   ├── model.py
│   ├── tools.py
│   ├── retrieval.py
│   ├── repositories.py
│   ├── unit_of_work.py
│   ├── tracing.py
│   └── events.py
│
├── bootstrap/
│   ├── __init__.py
│   └── runtime_factory.py
│
└── tests/
    ├── unit/
    │   ├── runtime/
    │   │   ├── test_kernel.py
    │   │   ├── test_phase_order.py
    │   │   ├── test_cleanup.py
    │   │   └── test_events.py
    │   └── application/
    │       └── test_agent_service.py
    └── architecture/
        └── test_dependency_boundaries.py
```

---

## 6. 运行时核心模型

以下代码是目标接口示例。实现时可以做合理调整，但应保持同等语义。

### 6.1 AgentRequest

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    attachment_id: str
    media_type: str
    name: str | None = None
    uri: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRequest:
    request_id: str
    session_id: str
    actor_id: str
    text: str
    attachments: tuple[AttachmentRef, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
```

要求：

- 不包含 Channel SDK 类型。
- 不包含 MessageBus Envelope。
- 不包含 Topic。
- 不包含 Redis / NATS / Kafka 字段。
- `metadata` 仅保存 Channel 无关或不影响核心运行的附加信息。

### 6.2 TurnStatus

```python
from enum import StrEnum


class TurnStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DENIED = "denied"
    WAITING_APPROVAL = "waiting_approval"
```

### 6.3 TurnResult

```python
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    call_id: str
    tool_name: str
    succeeded: bool
    duration_ms: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_id: str
    request_id: str
    session_id: str
    actor_id: str
    status: TurnStatus
    text: str
    usage: UsageSummary
    tool_records: tuple[ToolExecutionRecord, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### 6.4 Message 和检索模型

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


class RetrievedItemKind(StrEnum):
    PREFERENCE = "preference"
    HISTORY = "history"
    MEMORY = "memory"
    DOCUMENT = "document"
    USER_FACT = "user_fact"


@dataclass(frozen=True, slots=True)
class RetrievedItem:
    item_id: str
    kind: RetrievedItemKind
    content: str
    score: float
    source: str
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### 6.5 偏好与记忆候选

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class CandidateOperation(StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    IGNORE = "ignore"
    TENTATIVE = "tentative"


@dataclass(frozen=True, slots=True)
class PreferenceCandidate:
    key: str
    value: str | None
    operation: CandidateOperation
    confidence: float
    source_message_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    content: str
    confidence: float
    importance: float
    source_message_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SummaryCandidate:
    content: str
    confidence: float
    metadata: Mapping[str, object] = field(default_factory=dict)
```

---

## 7. TurnContext

`TurnContext` 是一次 Agent Turn 的强类型运行状态。

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from cogito_agent.runtime.models import (
    AgentRequest,
    TurnResult,
    TurnStatus,
    UsageSummary,
    ToolExecutionRecord,
)
from cogito_agent.domain.messages import ModelMessage
from cogito_agent.domain.retrieval import RetrievedItem
from cogito_agent.domain.preferences import PreferenceCandidate
from cogito_agent.domain.memory import MemoryCandidate, SummaryCandidate


@dataclass(slots=True)
class TurnContext:
    request: AgentRequest

    # Lifecycle
    turn_id: str | None = None
    status: TurnStatus = TurnStatus.CREATED
    started_at: datetime | None = None
    completed_at: datetime | None = None
    current_phase: str | None = None

    # Trace / cancellation / runtime limits
    trace_id: str | None = None
    cancellation_requested: bool = False
    max_tool_rounds: int = 8

    # Deterministic state
    session: object | None = None
    recent_messages: list[object] = field(default_factory=list)
    session_summary: object | None = None
    user_profile: object | None = None
    user_settings: dict[str, object] = field(default_factory=dict)

    # Retrieval
    retrieved_items: list[RetrievedItem] = field(default_factory=list)
    current_preferences: list[object] = field(default_factory=list)

    # Context assembly
    model_messages: list[ModelMessage] = field(default_factory=list)
    available_tools: list[object] = field(default_factory=list)

    # Agent loop
    model_responses: list[object] = field(default_factory=list)
    final_response: object | None = None
    output_text: str | None = None
    tool_records: list[ToolExecutionRecord] = field(default_factory=list)
    usage: UsageSummary = field(default_factory=UsageSummary)

    # Knowledge extraction
    preference_candidates: list[PreferenceCandidate] = field(default_factory=list)
    memory_candidates: list[MemoryCandidate] = field(default_factory=list)
    summary_candidate: SummaryCandidate | None = None

    # Persistence / final result
    persistence_completed: bool = False
    result: TurnResult | None = None

    # Failure information
    error: BaseException | None = None

    # Limited extension area
    metadata: dict[str, object] = field(default_factory=dict)
```

实现要求：

1. 不得使用 `slots: dict[str, Any]` 代替正式字段。
2. 允许领域类型暂时使用 `object` 占位，但应添加明确 TODO。
3. 后续实现各 Phase 时，再逐步替换为具体领域模型。
4. `TurnContext` 是本轮可变对象，不要求每个 Phase 创建全新的 Context。
5. Phase 之间不直接互相调用，只通过 Context 和注入的 Port 协作。

---

## 8. Phase 抽象

### 8.1 RuntimePhase Protocol

```python
from __future__ import annotations

from typing import Protocol

from cogito_agent.runtime.context import TurnContext


class RuntimePhase(Protocol):
    @property
    def name(self) -> str:
        ...

    async def run(self, ctx: TurnContext) -> None:
        ...
```

### 8.2 BasePhase

```python
from abc import ABC, abstractmethod


class BasePhase(ABC):
    name: str

    async def run(self, ctx: TurnContext) -> None:
        await self.execute(ctx)

    @abstractmethod
    async def execute(self, ctx: TurnContext) -> None:
        ...
```

要求：

- Phase 顺序只由外部列表决定。
- Phase 不声明 `requires`。
- Phase 不声明 `produces`。
- Phase 不执行拓扑排序。
- Phase 不扫描插件。
- Phase 名称必须唯一。
- Kernel 初始化时校验 Phase 名称重复。

### 8.3 Phase 空壳

请创建以下类：

```python
class TurnInitPhase(BasePhase):
    name = "turn_init"


class StateLoadPhase(BasePhase):
    name = "state_load"


class InformationRetrievalPhase(BasePhase):
    name = "information_retrieval"


class ContextAssemblyPhase(BasePhase):
    name = "context_assembly"


class AgentLoopPhase(BasePhase):
    name = "agent_loop"


class KnowledgeExtractionPhase(BasePhase):
    name = "knowledge_extraction"


class PersistencePhase(BasePhase):
    name = "persistence"


class TurnFinalizePhase(BasePhase):
    name = "turn_finalize"
```

本次只建立类、构造函数依赖和明确 TODO，不实现真实业务逻辑。

建议：

- `TurnInitPhase` 可以实现最小基础逻辑，例如生成 ID、设置开始时间和状态。
- `StateLoadPhase` 至 `PersistencePhase` 暂时只保留结构，不制造假数据。
- `AgentLoopPhase` 在没有真实实现时，应抛出清晰的 `PhaseNotImplementedError`，不要伪造模型回答。
- `TurnFinalizePhase` 只在 `ctx.output_text` 已存在时生成 `TurnResult`。
- 测试 Pipeline 时使用 Test Double Phase，不依赖这些未实现 Phase。

---

## 9. Agent Event

Kernel 可以通过抽象事件输出端发送运行事件，但不得知道事件最终是否进入 MessageBus。

### 9.1 Event Type

```python
from enum import StrEnum


class AgentEventType(StrEnum):
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"

    PHASE_STARTED = "phase_started"
    PHASE_COMPLETED = "phase_completed"
    PHASE_FAILED = "phase_failed"

    RETRIEVAL_STARTED = "retrieval_started"
    RETRIEVAL_COMPLETED = "retrieval_completed"

    MODEL_CALL_STARTED = "model_call_started"
    MODEL_DELTA = "model_delta"
    MODEL_CALL_COMPLETED = "model_call_completed"

    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"

    KNOWLEDGE_EXTRACTED = "knowledge_extracted"
    PERSISTENCE_COMPLETED = "persistence_completed"
```

### 9.2 AgentEvent

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: AgentEventType
    turn_id: str
    request_id: str
    timestamp: datetime
    phase: str | None = None
    data: Mapping[str, object] = field(default_factory=dict)
```

约束：

- `data` 不得包含 Channel SDK 对象。
- `data` 不得包含数据库连接。
- `data` 不得包含 Exception 对象本身。
- 错误事件应存放序列化后的错误代码和安全消息。
- 不要在事件中泄漏系统 Prompt、密钥、Token 或敏感用户信息。

### 9.3 AgentEventSink Port

```python
from typing import Protocol


class AgentEventSink(Protocol):
    async def emit(self, event: AgentEvent) -> None:
        ...
```

提供以下基础实现：

```python
class NullAgentEventSink:
    async def emit(self, event: AgentEvent) -> None:
        return None


class InMemoryAgentEventSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class CompositeAgentEventSink:
    def __init__(self, sinks: list[AgentEventSink]) -> None:
        self._sinks = list(sinks)

    async def emit(self, event: AgentEvent) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception:
                # Event consumer failure must not crash the Agent turn.
                # Use logging; do not silently ignore without trace.
                ...
```

Kernel 只依赖 `AgentEventSink` Protocol。

未来可实现：

```text
BusAgentEventSink
LoggingAgentEventSink
TraceAgentEventSink
TUIAgentEventSink
```

这些 Adapter 不得放在 `runtime/` 中。

---

## 10. RuntimeKernel

### 10.1 Kernel 接口

```python
from collections.abc import Sequence

from cogito_agent.ports.events import AgentEventSink
from cogito_agent.runtime.context import TurnContext
from cogito_agent.runtime.models import AgentRequest, TurnResult
from cogito_agent.runtime.phase import RuntimePhase


class RuntimeKernel:
    def __init__(
        self,
        phases: Sequence[RuntimePhase],
        *,
        default_event_sink: AgentEventSink | None = None,
        cleanup: RuntimeCleanup | None = None,
        error_mapper: RuntimeErrorMapper | None = None,
    ) -> None:
        ...
```

核心方法：

```python
async def run(
    self,
    request: AgentRequest,
    *,
    event_sink: AgentEventSink | None = None,
) -> TurnResult:
    ...
```

### 10.2 推荐执行逻辑

```python
async def run(
    self,
    request: AgentRequest,
    *,
    event_sink: AgentEventSink | None = None,
) -> TurnResult:
    sink = event_sink or self._default_event_sink
    ctx = TurnContext(request=request)

    async def emit_safely(event: AgentEvent) -> None:
        try:
            await sink.emit(event)
        except Exception:
            # Event delivery is observational and must not break the turn.
            logger.exception(
                "Agent event delivery failed",
                extra={
                    "event_type": event.type,
                    "request_id": request.request_id,
                },
            )

    try:
        ctx.status = TurnStatus.RUNNING

        await emit_safely(
            self._events.turn_started(ctx)
        )

        for phase in self._phases:
            ctx.current_phase = phase.name

            await emit_safely(
                self._events.phase_started(ctx, phase.name)
            )

            try:
                await phase.run(ctx)
            except Exception as exc:
                await emit_safely(
                    self._events.phase_failed(
                        ctx,
                        phase.name,
                        exc,
                    )
                )
                raise

            await emit_safely(
                self._events.phase_completed(
                    ctx,
                    phase.name,
                )
            )

        if ctx.result is None:
            raise MissingTurnResultError()

        ctx.status = TurnStatus.COMPLETED

        await emit_safely(
            self._events.turn_completed(ctx)
        )

        return ctx.result

    except asyncio.CancelledError:
        ctx.status = TurnStatus.CANCELLED
        raise

    except Exception as exc:
        ctx.status = TurnStatus.FAILED
        ctx.error = exc

        mapped = self._error_mapper.map(exc)

        await emit_safely(
            self._events.turn_failed(ctx, mapped)
        )

        raise mapped from exc

    finally:
        await self._cleanup.run(ctx)
```

### 10.3 Kernel 必须满足的行为

1. Phase 按传入列表顺序执行。
2. Phase 名称重复时初始化失败。
3. 任一 Phase 失败后，后续 Phase 不执行。
4. 任一 Phase 失败后，Cleanup 仍执行。
5. EventSink 失败原则上不应中断 Agent Turn。
6. `asyncio.CancelledError` 不应被包装成普通 Runtime Error。
7. Kernel 不导入 MessageBus 相关模块。
8. Kernel 不导入 Channel 相关模块。
9. Kernel 不实现业务检索、模型调用或数据库逻辑。
10. Kernel 不包含 `if phase.name == ...` 的业务分支。
11. Kernel 不固定只能有 8 个 Phase。
12. 后期插入第 9 个或第 10 个 Phase 时，Kernel 无需修改。

---

## 11. Cleanup 与错误处理

### 11.1 RuntimeCleanup

```python
from typing import Protocol


class RuntimeCleanup(Protocol):
    async def run(self, ctx: TurnContext) -> None:
        ...
```

默认实现可以负责：

- 结束 Trace / Span。
- 释放临时资源。
- 清理 Session Lock。
- 记录完成时间。
- 安全关闭本轮临时对象。

Cleanup 应满足：

- 幂等。
- 不抛出覆盖原始错误的新异常。
- 自己的异常需要记录日志。
- 无论成功、失败或取消都执行。

### 11.2 Runtime Error

建立统一错误基类：

```python
class RuntimeAgentError(Exception):
    code = "RUNTIME_ERROR"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        safe_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.safe_message = safe_message or message
```

建议预留：

```text
InvalidAgentRequestError
DuplicatePhaseNameError
PhaseNotImplementedError
PhaseExecutionError
MissingTurnResultError
ModelInvocationError
ToolExecutionError
MaxToolRoundsExceededError
RetrievalError
PersistenceError
PolicyDeniedError
ApprovalRequiredError
```

错误 Mapper 应将内部异常映射为稳定错误：

```python
@dataclass(frozen=True, slots=True)
class MappedRuntimeError:
    code: str
    safe_message: str
    retryable: bool
```

不得把完整堆栈或敏感内部信息发送到 Channel。

---

## 12. Port 接口

本次只建立 Protocol，不实现真实 Adapter。

### 12.1 时间和 ID

```python
class ClockPort(Protocol):
    def now(self) -> datetime:
        ...


class IdGeneratorPort(Protocol):
    def new_id(self) -> str:
        ...
```

### 12.2 模型

```python
class ModelPort(Protocol):
    async def generate(
        self,
        *,
        messages: list[ModelMessage],
        tools: list[object],
    ) -> object:
        ...
```

后续若需要流式输出，可以扩展为独立方法或流式响应接口，但初始框架不要实现两套重复 Agent Loop。

### 12.3 工具

```python
class ToolCatalogPort(Protocol):
    async def list_available_tools(
        self,
        *,
        actor_id: str,
        session_id: str,
    ) -> list[object]:
        ...


class ToolExecutorPort(Protocol):
    async def execute(
        self,
        *,
        tool_call: object,
        context: object,
    ) -> object:
        ...
```

### 12.4 检索

```python
@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    actor_id: str
    session_id: str
    text: str
    limit: int = 20


class RetrieverPort(Protocol):
    @property
    def name(self) -> str:
        ...

    async def retrieve(
        self,
        query: RetrievalQuery,
    ) -> list[RetrievedItem]:
        ...


class RetrievalFusionPort(Protocol):
    def merge(
        self,
        result_groups: list[list[RetrievedItem]],
    ) -> list[RetrievedItem]:
        ...


class RetrievalRerankerPort(Protocol):
    async def rerank(
        self,
        *,
        query: RetrievalQuery,
        items: list[RetrievedItem],
    ) -> list[RetrievedItem]:
        ...
```

后续实现：

```text
KeywordRetrieverAdapter
VectorRetrieverAdapter
PreferenceRetrieverAdapter
HistoryRetrieverAdapter
MemoryRetrieverAdapter
```

### 12.5 Repository

```python
class SessionRepositoryPort(Protocol):
    async def get(self, session_id: str) -> object | None:
        ...


class MessageRepositoryPort(Protocol):
    async def list_recent(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[object]:
        ...

    async def save_turn(self, turn: object) -> None:
        ...


class PreferenceRepositoryPort(Protocol):
    async def list_for_actor(
        self,
        actor_id: str,
    ) -> list[object]:
        ...

    async def apply_candidates(
        self,
        *,
        actor_id: str,
        candidates: list[PreferenceCandidate],
    ) -> None:
        ...


class MemoryRepositoryPort(Protocol):
    async def save_candidates(
        self,
        *,
        actor_id: str,
        candidates: list[MemoryCandidate],
    ) -> None:
        ...


class SummaryRepositoryPort(Protocol):
    async def get(self, session_id: str) -> object | None:
        ...

    async def update(
        self,
        *,
        session_id: str,
        candidate: SummaryCandidate,
    ) -> None:
        ...
```

### 12.6 Unit of Work

```python
class UnitOfWorkPort(Protocol):
    async def __aenter__(self) -> "UnitOfWorkPort":
        ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        ...

    async def commit(self) -> None:
        ...

    async def rollback(self) -> None:
        ...
```

### 12.7 Trace

```python
class RuntimeTracePort(Protocol):
    async def start_turn(
        self,
        *,
        turn_id: str,
        request_id: str,
    ) -> str:
        ...

    async def end_turn(
        self,
        *,
        trace_id: str,
        status: str,
    ) -> None:
        ...
```

---

## 13. Application Service

Application Service 提供稳定入口，不包含 MessageBus 具体实现。

```python
class AgentApplicationService:
    def __init__(self, kernel: RuntimeKernel) -> None:
        self._kernel = kernel

    async def process(
        self,
        request: AgentRequest,
        *,
        event_sink: AgentEventSink | None = None,
    ) -> TurnResult:
        return await self._kernel.run(
            request,
            event_sink=event_sink,
        )
```

后续如果需要同步入口，必须放在最外层 CLI Adapter，不要在 Kernel 内部使用 `asyncio.run()`。

---

## 14. MessageBus 边界

本次建立接口和 DTO，可以不实现真实 Bus。

### 14.1 MessageEnvelope

`MessageEnvelope` 属于 Application Messaging 层，不属于 Runtime Domain。

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    message_id: str
    message_type: str
    correlation_id: str
    source: str
    reply_to: str | None
    timestamp: datetime
    payload: Mapping[str, object]
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### 14.2 Mapper

```python
class AgentRequestMapper(Protocol):
    def to_request(
        self,
        envelope: MessageEnvelope,
    ) -> AgentRequest:
        ...


class AgentOutputMapper(Protocol):
    def event_to_envelope(
        self,
        *,
        source: MessageEnvelope,
        event: AgentEvent,
    ) -> MessageEnvelope:
        ...

    def result_to_envelope(
        self,
        *,
        source: MessageEnvelope,
        result: TurnResult,
    ) -> MessageEnvelope:
        ...

    def error_to_envelope(
        self,
        *,
        source: MessageEnvelope,
        error: MappedRuntimeError,
    ) -> MessageEnvelope:
        ...
```

### 14.3 Publisher Port

```python
class MessagePublisherPort(Protocol):
    async def publish(
        self,
        *,
        destination: str,
        envelope: MessageEnvelope,
    ) -> None:
        ...
```

### 14.4 AgentMessageWorker

```python
class AgentMessageWorker:
    def __init__(
        self,
        *,
        service: AgentApplicationService,
        request_mapper: AgentRequestMapper,
        output_mapper: AgentOutputMapper,
        publisher: MessagePublisherPort,
    ) -> None:
        self._service = service
        self._request_mapper = request_mapper
        self._output_mapper = output_mapper
        self._publisher = publisher

    async def handle(
        self,
        envelope: MessageEnvelope,
    ) -> None:
        request = self._request_mapper.to_request(envelope)

        sink = BusAgentEventSink(
            source=envelope,
            mapper=self._output_mapper,
            publisher=self._publisher,
        )

        try:
            result = await self._service.process(
                request,
                event_sink=sink,
            )
        except RuntimeAgentError as exc:
            error_envelope = self._output_mapper.error_to_envelope(
                source=envelope,
                error=map_error(exc),
            )
            await self._publisher.publish(
                destination=envelope.reply_to or "agent.output",
                envelope=error_envelope,
            )
            return

        result_envelope = self._output_mapper.result_to_envelope(
            source=envelope,
            result=result,
        )

        await self._publisher.publish(
            destination=envelope.reply_to or "agent.output",
            envelope=result_envelope,
        )
```

注意：

- Worker 同时知道 MessageBus Port 和 Application Service。
- Kernel 不知道 Worker。
- Kernel 不知道 Envelope。
- Channel Adapter 不直接调用 Kernel。
- Channel Adapter 只发布和消费 MessageBus 消息。

---

## 15. 输出消息建议

后续连接 MessageBus 时，至少区分：

```text
agent.turn.started
agent.phase.started
agent.phase.completed
agent.model.delta
agent.tool.started
agent.tool.completed
agent.turn.completed
agent.turn.failed
```

最终结果消息示例：

```json
{
  "message_type": "agent.turn.completed",
  "correlation_id": "original-correlation-id",
  "source": "agent",
  "payload": {
    "turn_id": "turn-123",
    "request_id": "request-123",
    "session_id": "session-123",
    "status": "completed",
    "text": "最终回答",
    "usage": {
      "input_tokens": 0,
      "output_tokens": 0,
      "total_tokens": 0,
      "model_calls": 0,
      "tool_calls": 0
    }
  }
}
```

失败消息示例：

```json
{
  "message_type": "agent.turn.failed",
  "correlation_id": "original-correlation-id",
  "source": "agent",
  "payload": {
    "error_code": "MODEL_INVOCATION_ERROR",
    "message": "模型调用失败",
    "retryable": true
  }
}
```

---

## 16. Composition Root

Composition Root 负责创建并按明确顺序组装 Phase。

```python
def build_runtime_kernel(
    *,
    clock: ClockPort,
    id_generator: IdGeneratorPort,
    event_sink: AgentEventSink | None = None,
) -> RuntimeKernel:
    phases: list[RuntimePhase] = [
        TurnInitPhase(
            clock=clock,
            id_generator=id_generator,
        ),
        StateLoadPhase(
            # repositories are injected later
        ),
        InformationRetrievalPhase(
            # retrievers/fusion/reranker are injected later
        ),
        ContextAssemblyPhase(
            # context builder/tool catalog are injected later
        ),
        AgentLoopPhase(
            # model/tool executor are injected later
        ),
        KnowledgeExtractionPhase(
            # extractor is injected later
        ),
        PersistencePhase(
            # repositories/uow are injected later
        ),
        TurnFinalizePhase(),
    ]

    return RuntimeKernel(
        phases=phases,
        default_event_sink=event_sink or NullAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
```

初始阶段如因未实现的业务依赖无法完整运行，可以另外提供测试工厂：

```python
def build_test_kernel(
    phases: Sequence[RuntimePhase],
) -> RuntimeKernel:
    return RuntimeKernel(
        phases=phases,
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
```

禁止在 RuntimeKernel 内部直接实例化 Repository、Model Adapter 或 MessageBus Adapter。

---

## 17. Phase 扩充方式

未来增加 Phase 时，只修改 Composition Root。

例如增加输入策略检查：

```python
phases = [
    TurnInitPhase(...),
    StateLoadPhase(...),
    InputPolicyPhase(...),
    InformationRetrievalPhase(...),
    ContextAssemblyPhase(...),
    AgentLoopPhase(...),
    KnowledgeExtractionPhase(...),
    PersistencePhase(...),
    TurnFinalizePhase(...),
]
```

例如增加人工审批：

```python
phases = [
    TurnInitPhase(...),
    StateLoadPhase(...),
    InformationRetrievalPhase(...),
    ContextAssemblyPhase(...),
    PlanningPhase(...),
    ApprovalPhase(...),
    AgentLoopPhase(...),
    KnowledgeExtractionPhase(...),
    PersistencePhase(...),
    TurnFinalizePhase(...),
]
```

Kernel 主循环不做任何修改。

需要条件跳过时，由 Phase 自己根据明确的 Context 状态执行 no-op：

```python
class ApprovalPhase(BasePhase):
    name = "approval"

    async def execute(self, ctx: TurnContext) -> None:
        if not ctx.metadata.get("requires_approval", False):
            return

        ...
```

不要加入通用 DAG、拓扑排序或隐式依赖系统。

---

## 18. 测试要求

使用 `pytest` 和 `pytest-asyncio`。

### 18.1 Phase 顺序

创建 `RecordingPhase`：

```python
class RecordingPhase:
    def __init__(self, name: str, records: list[str]) -> None:
        self.name = name
        self._records = records

    async def run(self, ctx: TurnContext) -> None:
        self._records.append(self.name)
```

验证：

```python
assert records == [
    "turn_init",
    "state_load",
    "information_retrieval",
    "context_assembly",
    "agent_loop",
    "knowledge_extraction",
    "persistence",
    "turn_finalize",
]
```

### 18.2 可扩充

插入自定义 Phase，验证 Kernel 无需修改即可执行。

```python
phases.insert(3, RecordingPhase("custom_phase", records))
```

### 18.3 重复名称

两个 Phase 使用相同名称时，Kernel 初始化应抛：

```text
DuplicatePhaseNameError
```

### 18.4 Phase 失败

某个 Phase 抛异常时：

- 后续 Phase 不执行。
- 发出 `PHASE_FAILED`。
- 发出 `TURN_FAILED`。
- Cleanup 执行。
- 原始错误被映射为稳定 Runtime Error。

### 18.5 Cleanup

分别测试：

- 成功路径执行 Cleanup。
- Phase 失败执行 Cleanup。
- Task 取消执行 Cleanup。
- Cleanup 自身错误不会覆盖原始错误。

### 18.6 Event 顺序

正常路径至少验证：

```text
TURN_STARTED
PHASE_STARTED(turn_init)
PHASE_COMPLETED(turn_init)
...
PHASE_STARTED(turn_finalize)
PHASE_COMPLETED(turn_finalize)
TURN_COMPLETED
```

失败路径至少验证：

```text
TURN_STARTED
PHASE_STARTED(...)
PHASE_FAILED(...)
TURN_FAILED
```

### 18.7 EventSink 故障隔离

一个 EventSink 抛异常时，不应导致正常 Phase 执行失败。

### 18.8 MessageBus 解耦

增加架构测试或静态检查，确保：

```text
cogito_agent/runtime/
```

不得导入：

```text
cogito_agent/application/messaging/
redis
nats
kafka
rabbitmq
telegram
discord
fastapi
starlette
```

### 18.9 Channel 解耦

`AgentRequest`、`TurnContext`、`RuntimeKernel` 中不得出现 Channel SDK 类型。

### 18.10 Application Service

验证 `AgentApplicationService.process()` 原样委托给 Kernel。

---

## 19. 代码质量要求

1. Python 3.12+。
2. 全部公共接口包含类型注解。
3. 使用 `from __future__ import annotations`。
4. 优先使用 `dataclass(slots=True)`。
5. 不滥用继承；Port 使用 `Protocol`。
6. Runtime Phase 可使用抽象基类。
7. 不使用全局 Service Locator。
8. 不在模块 import 时创建连接或启动后台任务。
9. 不在 Kernel 中调用 `asyncio.run()`。
10. 不静默吞掉异常。
11. 不使用裸 `except:`.
12. 不把所有 DTO 都定义成 `dict[str, Any]`。
13. 不在领域层导入基础设施实现。
14. 不为未来不确定需求提前创建复杂插件系统。
15. 不创建拓扑排序器。
16. 不创建动态 Module Registry。
17. 不创建分布式锁或任务队列。
18. 不制造假的模型结果来让框架看似可用。
19. 未实现的业务能力应明确抛 `PhaseNotImplementedError`。
20. 测试应使用 Fake / Stub / Recording Phase。

---

## 20. 本次交付内容

实现型 AI 应提交：

### 20.1 Runtime

- `AgentRequest`
- `TurnContext`
- `TurnResult`
- `AgentEvent`
- `AgentEventType`
- `RuntimePhase`
- `BasePhase`
- `RuntimeKernel`
- `RuntimeCleanup`
- `RuntimeErrorMapper`
- Runtime Error 类型

### 20.2 八个 Phase 空壳

- `TurnInitPhase`
- `StateLoadPhase`
- `InformationRetrievalPhase`
- `ContextAssemblyPhase`
- `AgentLoopPhase`
- `KnowledgeExtractionPhase`
- `PersistencePhase`
- `TurnFinalizePhase`

### 20.3 Ports

- Clock
- ID Generator
- Event Sink
- Model
- Tool Catalog
- Tool Executor
- Retriever
- Fusion
- Reranker
- Repository
- Unit of Work
- Trace

### 20.4 Application

- `AgentApplicationService`

### 20.5 Messaging 预留框架

- `MessageEnvelope`
- `AgentRequestMapper`
- `AgentOutputMapper`
- `MessagePublisherPort`
- `AgentMessageWorker`
- `BusAgentEventSink` 接口或空壳

不得实现具体 Redis / NATS / RabbitMQ Adapter。

### 20.6 Bootstrap

- `build_runtime_kernel()`
- `build_test_kernel()`

### 20.7 Tests

- Phase 顺序。
- Phase 可插入。
- 重复 Phase 名称。
- Phase 失败。
- Cleanup。
- Event 顺序。
- EventSink 故障隔离。
- MessageBus / Channel 依赖边界。
- Application Service 委托行为。

---

## 21. 验收标准

完成后必须满足：

- [ ] Kernel 只依赖领域模型、Phase 和 Port。
- [ ] Kernel 不依赖 Channel。
- [ ] Kernel 不依赖 MessageBus。
- [ ] Phase 顺序由一个显式列表定义。
- [ ] 没有拓扑排序代码。
- [ ] 没有 `requires` / `produces` Slot 机制。
- [ ] 可以通过修改 Composition Root 插入新 Phase。
- [ ] Kernel 主循环不需要因新增 Phase 而修改。
- [ ] 八个初始 Phase 均有独立类。
- [ ] `TurnContext` 具有强类型核心字段。
- [ ] 检索结果和偏好候选有明确模型。
- [ ] 知识抽取与数据库持久化是两个不同 Phase。
- [ ] MessageBus Worker 位于 Kernel 外部。
- [ ] EventSink 只是抽象 Port。
- [ ] Cleanup 在成功、失败和取消路径都执行。
- [ ] 未实现的业务逻辑不会返回伪造成功结果。
- [ ] 单元测试全部通过。
- [ ] 架构边界测试通过。
- [ ] 代码可以通过类型检查和 lint。

---

## 22. 实现顺序建议

请按以下顺序实现，避免一次性写入过多业务细节：

### Step 1：领域 DTO

先实现：

- `AgentRequest`
- `TurnResult`
- `AgentEvent`
- `TurnContext`
- Retrieval / Preference / Memory DTO

### Step 2：基础 Port

实现 Protocol：

- Clock
- IDs
- EventSink
- Cleanup
- ErrorMapper

### Step 3：Phase 抽象

实现：

- `RuntimePhase`
- `BasePhase`
- Phase 名称校验
- 八个 Phase 空壳

### Step 4：RuntimeKernel

实现：

- 固定顺序执行。
- Event 生命周期。
- 错误映射。
- Cleanup。
- 取消处理。

### Step 5：Application Service

实现稳定应用入口。

### Step 6：Messaging 预留框架

只实现 DTO、Port 和 Worker 结构，不接真实 Bus。

### Step 7：Composition Root

创建生产框架工厂和测试工厂。

### Step 8：测试

优先完成 Kernel 和架构边界测试。

---

## 23. 最终说明

本框架的目标不是一次性构建完整通用 Agent 平台，而是建立一个清晰、可测试、可逐步实现的运行骨架。

第一阶段最重要的是保持以下边界：

```text
Channel
    ↓
MessageBus
    ↓
AgentMessageWorker
    ↓
AgentApplicationService
    ↓
RuntimeKernel
    ↓
Ordered Phases
    ↓
Ports
```

八个 Phase 只是当前默认组合，不是 Kernel 的硬编码限制。

未来可以继续增加：

- InputPolicyPhase
- PlanningPhase
- ApprovalPhase
- SafetyEvaluationPhase
- ResponseValidationPhase
- ReflectionPhase

扩充方式始终是修改 Composition Root 中的显式列表，而不是引入拓扑排序或隐式 Module 依赖。

各 Phase 的具体业务实现，包括模型调用、关键词检索、向量检索、偏好检索、知识抽取和数据库持久化，将在后续任务中逐个完成。
