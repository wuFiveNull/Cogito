# Cogito-Agent `InformationRetrievalPhase` 最终实现设计

> 本文档定义 `InformationRetrievalPhase` 的完整实现路径、稳定接口、执行语义、错误边界、融合算法、权限规则、可观测性与测试验收标准，可直接作为实现任务书。

---

## 1. 设计目标

`InformationRetrievalPhase` 位于 `StateLoadPhase` 之后、`ContextAssemblyPhase` 之前，唯一职责是：

1. 根据当前请求与已加载的确定性状态构建检索查询。
2. 按显式配置选择检索源。
3. 受控并发执行关键词、向量、偏好、历史和长期记忆检索。
4. 对结果执行格式校验、权限过滤、去重、融合、重排和多样性裁剪。
5. 将最终结果写入 `TurnContext.retrieved_items`。
6. 将强类型诊断信息写入 `TurnContext.retrieval_diagnostics`。
7. 在可选检索源失败时按策略降级，在必要检索源失败时抛出稳定 Runtime Error。

本 Phase 不负责：

- 构建最终模型 Messages。
- 分配最终 Prompt Token Budget。
- 调用模型生成用户回答。
- 执行工具。
- 提取新的用户偏好或长期记忆。
- 写数据库或提交事务。
- 发布 MessageBus 消息。
- 感知 Telegram、Discord、HTTP、WebSocket 等 Channel 类型。

---

## 2. 最终架构决策

### 2.1 一个 Phase，多个内部组件

检索的不同策略不拆成顶层 Phase。最终结构如下：

```text
InformationRetrievalPhase
├── RetrievalQueryBuilder
├── RetrievalRoutingPolicy
├── RetrieverPort[]
│   ├── KeywordRetrieverAdapter
│   ├── VectorRetrieverAdapter
│   ├── PreferenceRetrieverAdapter
│   ├── HistoryRetrieverAdapter
│   └── LongTermMemoryRetrieverAdapter
├── RetrievalItemValidator
├── RetrievalAccessFilterPort
├── RetrievalNormalizer
├── RetrievalFusionPort
├── RetrievalRerankerPort
├── RetrievalSelector
└── RetrievalDiagnosticsBuilder
```

### 2.2 检索源必须显式注入

禁止：

- 扫描模块自动发现 Retriever。
- 使用全局 Registry 或 Service Locator。
- 根据类名或字符串隐式加载实现。
- 在 Phase 内直接实例化数据库、向量库或搜索引擎客户端。

检索源通过 Composition Root 显式传入，并在 Phase 构造时校验名称唯一。

### 2.3 原始分数不得直接跨源比较

BM25、余弦相似度、数据库置信度和时间衰减分数不在同一标度上。跨源融合使用**加权倒数排名融合**（Weighted Reciprocal Rank Fusion，Weighted RRF），而不是直接对原始分数求和。

### 2.4 权限过滤执行两次

1. **源端过滤**：Adapter 查询时必须携带 actor、session、tenant/namespace 等访问上下文，只查询允许访问的数据。
2. **Phase 防御性过滤**：结果进入融合前再次检查 ACL；重排后再执行最终检查，防止 Adapter 或 Reranker 引入越权项。

### 2.5 部分失败可降级，取消必须传播

- 可选源失败或超时：记录失败，继续使用其他成功源。
- 必要源失败：Phase 失败。
- 所有已启用源均失败：Phase 失败。
- 查询成功但无结果：正常完成，写入空列表。
- `asyncio.CancelledError`：不捕获、不包装，直接向 Kernel 传播。

### 2.6 最终结果顺序必须确定

相同输入、相同源结果和相同配置下，输出顺序必须稳定。所有排序都必须包含稳定 tie-break：

```text
final_score DESC
kind_priority ASC
source ASC
item_id ASC
```

---

## 3. 目录结构

在现有工程结构上增加检索编排组件和基础设施 Adapter 目录：

```text
cogito_agent/
├── domain/
│   └── retrieval.py
├── ports/
│   └── retrieval.py
├── retrieval/
│   ├── __init__.py
│   ├── query_builder.py
│   ├── routing.py
│   ├── validation.py
│   ├── normalization.py
│   ├── fusion.py
│   ├── selection.py
│   └── diagnostics.py
├── runtime/
│   ├── context.py
│   ├── errors.py
│   └── phases/
│       └── information_retrieval.py
├── infrastructure/
│   └── retrieval/
│       ├── __init__.py
│       ├── keyword.py
│       ├── vector.py
│       ├── preference.py
│       ├── history.py
│       └── long_term_memory.py
├── bootstrap/
│   └── runtime_factory.py
└── tests/
    ├── unit/
    │   ├── retrieval/
    │   │   ├── test_query_builder.py
    │   │   ├── test_routing.py
    │   │   ├── test_validation.py
    │   │   ├── test_fusion.py
    │   │   └── test_selection.py
    │   └── runtime/phases/
    │       └── test_information_retrieval.py
    ├── contract/
    │   └── retrieval/
    │       └── test_retriever_contract.py
    ├── integration/
    │   └── test_information_retrieval_pipeline.py
    └── architecture/
        └── test_retrieval_dependency_boundaries.py
```

依赖方向：

```text
runtime/phases/information_retrieval.py
    → domain/retrieval.py
    → ports/retrieval.py
    → retrieval/*

infrastructure/retrieval/*
    → ports/retrieval.py
    → domain/retrieval.py
```

`domain/`、`ports/`、`retrieval/` 和 `runtime/` 不得导入具体搜索引擎、向量数据库、ORM、MessageBus 或 Channel SDK。

---

## 4. 领域模型

现有规格中的检索 DTO 是占位定义。具体实现时应替换为以下强类型模型。

### 4.1 枚举

```python
from __future__ import annotations

from enum import StrEnum


class RetrievedItemKind(StrEnum):
    PREFERENCE = "preference"
    HISTORY = "history"
    MEMORY = "memory"
    DOCUMENT = "document"
    USER_FACT = "user_fact"


class RetrievalFailureKind(StrEnum):
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    PERMISSION = "permission"
    INTERNAL = "internal"


class RetrievalCompletionStatus(StrEnum):
    COMPLETED = "completed"
    DEGRADED = "degraded"
    EMPTY = "empty"
```

### 4.2 访问范围

```python
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class RetrievalAccessContext:
    actor_id: str
    session_id: str
    tenant_id: str | None = None
    namespace: str | None = None
    roles: tuple[str, ...] = ()
    attributes: Mapping[str, str] = field(default_factory=dict)
```

约束：

- `tenant_id`、`namespace` 和 `roles` 来自可信的 Application 映射或已验证状态，不得直接相信用户输入文本。
- Adapter 必须用该对象限制查询范围。
- 任何源都不得返回其他 actor 的私有偏好、私有记忆或私有会话内容。

### 4.3 查询过滤条件

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    kinds: tuple[RetrievedItemKind, ...] = ()
    created_after: datetime | None = None
    created_before: datetime | None = None
    tags: tuple[str, ...] = ()
    language: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
```

### 4.4 检索查询

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    request_id: str
    turn_id: str
    text: str
    access: RetrievalAccessContext
    filters: RetrievalFilters
    limit: int
    locale: str | None = None
```

约束：

- `text` 是规范化后的检索文本，不是最终 Prompt。
- `limit` 是全局候选目标，不等于每个源的最终返回上限。
- Query 不携带数据库连接、ORM 对象、Channel DTO 或 MessageEnvelope。

### 4.5 检索路由

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalRoute:
    source: str
    limit: int
    timeout_seconds: float
    weight: float
    required: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    query: RetrievalQuery
    routes: tuple[RetrievalRoute, ...]
```

### 4.6 来源信息

```python
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class RetrievalProvenance:
    source: str
    source_item_id: str
    source_rank: int
    raw_score: float | None = None
    uri: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
```

`metadata` 不得包含：

- 数据库连接或 ORM Session。
- 凭证、密钥、Token。
- 未脱敏的内部查询语句。
- Exception 对象。
- Channel SDK 对象。

### 4.7 检索结果

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class RetrievedItem:
    item_id: str
    kind: RetrievedItemKind
    content: str
    source: str

    # Phase 处理完成后的统一相关性分数，范围 [0.0, 1.0]
    score: float

    # 跨源去重键；Adapter 不提供时由 Phase 生成
    dedupe_key: str | None = None

    # 可选时间信息，用于过滤、时效性排序和审计
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # 多源合并后保留完整来源链
    provenance: tuple[RetrievalProvenance, ...] = ()

    metadata: Mapping[str, object] = field(default_factory=dict)
```

重要语义：

- `score` 在 Adapter 返回时可暂时表示源内归一化分数；融合后必须覆盖为统一分数。
- `content` 必须是供后续 Context Assembly 使用的规范化文本，不包含不可序列化对象。
- 相同事实由多个源命中时，只保留一个 `RetrievedItem`，并把多个来源合并到 `provenance`。

### 4.8 单源返回批次

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalBatch:
    source: str
    items: tuple[RetrievedItem, ...]
    partial: bool = False
```

### 4.9 单源失败与总体诊断

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalSourceFailure:
    source: str
    kind: RetrievalFailureKind
    error_code: str
    safe_message: str
    retryable: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RetrievalSourceStats:
    source: str
    duration_ms: int
    received_count: int
    accepted_count: int
    rejected_count: int
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostics:
    status: RetrievalCompletionStatus
    total_duration_ms: int
    selected_sources: tuple[str, ...]
    successful_sources: tuple[str, ...]
    source_stats: tuple[RetrievalSourceStats, ...]
    failures: tuple[RetrievalSourceFailure, ...]
    pre_fusion_count: int
    post_fusion_count: int
    final_count: int
    reranker_used: bool
    reranker_degraded: bool
```

诊断对象不保存检索正文，避免日志和事件泄漏用户信息。

---

## 5. `TurnContext` 修改

在 `TurnContext` 中增加正式字段，禁止将诊断对象塞进通用 `metadata`：

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class TurnContext:
    # ...现有字段...

    retrieved_items: list[RetrievedItem] = field(default_factory=list)
    retrieval_diagnostics: RetrievalDiagnostics | None = None

    # Kernel 在每次 run 时注入的本轮事件发射器。
    # Phase 不直接持有 MessageBus 或具体 EventSink。
    event_emitter: TurnEventEmitterPort | None = None
```

Phase 执行前的前置条件：

- `ctx.turn_id` 已由 `TurnInitPhase` 生成。
- `ctx.request.request_id`、`actor_id`、`session_id` 已通过基础校验。
- `StateLoadPhase` 已完成；`session`、`user_profile`、`user_settings`、`recent_messages` 等字段可以为空，但必须具有确定语义。

Phase 完成后的后置条件：

- `ctx.retrieved_items` 一定是 list，可为空。
- `ctx.retrieval_diagnostics` 一定非空。
- 所有 `RetrievedItem.score` 均在 `[0.0, 1.0]`。
- 所有结果均通过访问过滤和结构校验。
- 输出顺序稳定。

---

## 6. Port 接口

### 6.1 Retriever Port

```python
from typing import Protocol


class RetrieverPort(Protocol):
    @property
    def name(self) -> str:
        ...

    async def retrieve(
        self,
        *,
        query: RetrievalQuery,
        limit: int,
    ) -> RetrievalBatch:
        ...
```

契约：

1. `name` 在进程内唯一且稳定。
2. 返回的 `RetrievalBatch.source` 必须等于 `name`。
3. `items` 必须按源内相关性从高到低排列。
4. Adapter 不得返回超过 `limit` 的结果。
5. Adapter 必须执行源端 ACL 限制。
6. Adapter 不得吞掉取消信号。
7. Adapter 将基础设施异常映射为检索 Adapter 异常，不泄漏供应商异常类型到 Runtime。
8. 无结果返回空批次，不抛异常。

### 6.2 Access Filter Port

```python
from typing import Protocol, Sequence


class RetrievalAccessFilterPort(Protocol):
    async def filter(
        self,
        *,
        access: RetrievalAccessContext,
        items: Sequence[RetrievedItem],
    ) -> list[RetrievedItem]:
        ...
```

默认实现可以是纯内存规则；涉及外部授权服务时保持异步。

### 6.3 Fusion Port

```python
from typing import Protocol, Sequence


class RetrievalFusionPort(Protocol):
    def merge(
        self,
        *,
        batches: Sequence[RetrievalBatch],
        routes: Sequence[RetrievalRoute],
    ) -> list[RetrievedItem]:
        ...
```

Fusion 必须：

- 跨源去重。
- 合并 provenance。
- 使用源排名而非直接混加 raw score。
- 输出统一 `[0.0, 1.0]` 分数。
- 保持确定性排序。

### 6.4 Reranker Port

```python
from typing import Protocol, Sequence


class RetrievalRerankerPort(Protocol):
    async def rerank(
        self,
        *,
        query: RetrievalQuery,
        items: Sequence[RetrievedItem],
        limit: int,
    ) -> list[RetrievedItem]:
        ...
```

Reranker 契约：

- 只能返回输入集合中的项，不得凭空创建新文档。
- 不得修改 `item_id`、`kind`、`content`、`source` 或 ACL 元数据。
- 只允许调整顺序和统一分数。
- 返回结果不得重复。
- 返回数量不得超过 `limit`。
- 分数必须在 `[0.0, 1.0]`。

### 6.5 可选的 No-op Reranker

```python
class IdentityRetrievalReranker:
    async def rerank(
        self,
        *,
        query: RetrievalQuery,
        items: Sequence[RetrievedItem],
        limit: int,
    ) -> list[RetrievedItem]:
        return list(items[:limit])
```

该实现不是伪造检索能力，只表示系统明确配置为不执行二次重排。

### 6.6 本轮事件发射 Port

Phase 需要发出检索专用事件，但不能直接持有 MessageBus，也不能在构造时固定某个 EventSink，因为 `RuntimeKernel.run()` 允许传入本轮专用 sink。Kernel 应创建 request-scoped emitter 并写入 `TurnContext.event_emitter`。

```python
from typing import Mapping, Protocol

from cogito_agent.runtime.events import AgentEventType


class TurnEventEmitterPort(Protocol):
    async def emit(
        self,
        *,
        event_type: AgentEventType,
        phase: str | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        ...
```

`RuntimeKernel` 的职责：

1. 根据本轮选定的 `AgentEventSink` 创建 `TurnEventEmitter`。
2. Emitter 内部绑定 `request_id`、`turn_id`、Clock 和安全事件工厂。
3. 将 Emitter 写入 `ctx.event_emitter`。
4. Emitter 复用 Kernel 的事件故障隔离策略，sink 失败不得中断 Turn。

Phase 只调用 `ctx.event_emitter.emit(...)`，不构造 MessageEnvelope，不知道事件最终去向。若未配置 emitter，Phase 安全跳过专用事件；Kernel 的通用 `PHASE_STARTED`/`PHASE_COMPLETED` 仍然存在。

---

## 7. 配置模型

配置使用强类型 dataclass，并在 Composition Root 启动时校验。

```python
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RetrievalSourceConfig:
    enabled: bool = True
    limit: int = 20
    timeout_seconds: float = 1.5
    weight: float = 1.0
    required: bool = False


@dataclass(frozen=True, slots=True)
class InformationRetrievalConfig:
    phase_timeout_seconds: float = 3.0
    max_concurrency: int = 5
    final_limit: int = 20
    rerank_candidate_limit: int = 60
    reranker_timeout_seconds: float = 1.5
    reranker_fail_open: bool = True
    rrf_k: int = 60
    max_content_chars: int = 20_000
    max_per_kind: int = 8
    max_per_source: int = 10
    empty_query_allowed: bool = True
    sources: dict[str, RetrievalSourceConfig] = field(default_factory=dict)
```

启动时必须校验：

- `phase_timeout_seconds > 0`。
- `max_concurrency >= 1`。
- `final_limit >= 1`。
- `rerank_candidate_limit >= final_limit`。
- `rrf_k >= 1`。
- 每个源的 `limit >= 1`、`timeout_seconds > 0`、`weight > 0`。
- 配置中启用的源必须存在于注入的 Retriever 集合。
- Retriever 名称不得重复。
- `max_per_kind` 和 `max_per_source` 不得小于 1。

配置必须来自普通配置层，不得让 Runtime 直接依赖特定配置框架。

---

## 8. 查询构建

### 8.1 `RetrievalQueryBuilder`

Query Builder 是纯组件，不执行 I/O：

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalQueryBuilder:
    default_limit: int

    def build(self, ctx: TurnContext) -> RetrievalQuery:
        if ctx.turn_id is None:
            raise InvalidRetrievalContextError(
                "turn_id is required before retrieval"
            )

        text = self._normalize_text(ctx.request.text)
        access = self._build_access_context(ctx)
        filters = self._build_filters(ctx)

        return RetrievalQuery(
            request_id=ctx.request.request_id,
            turn_id=ctx.turn_id,
            text=text,
            access=access,
            filters=filters,
            limit=self.default_limit,
            locale=self._resolve_locale(ctx),
        )
```

### 8.2 文本规范化规则

只做确定性规范化：

1. Unicode 规范化为 NFKC。
2. 去除首尾空白。
3. 连续空白折叠为一个空格。
4. 保留原语言和语义标点。
5. 限制最大查询长度；超长文本截断时记录诊断标记，不记录全文。
6. 不执行最终 Prompt 拼接。
7. 不把完整近期历史无差别拼入查询。
8. 不把敏感偏好、密钥或系统提示词拼入查询。

Query Builder 可以依据可信元数据构建过滤条件，例如：

- 语言。
- 时间范围。
- 文档标签。
- namespace。
- 允许的 `RetrievedItemKind`。

不得依据用户文本直接提升 ACL 权限。

### 8.3 空文本规则

当 `request.text` 为空但请求包含附件或系统明确允许无文本 Turn 时：

- 偏好、近期历史等不依赖文本的源可以执行。
- 关键词和向量源由 Routing Policy 决定跳过。
- 无路由时正常返回空检索结果。

---

## 9. 路由策略

`RetrievalRoutingPolicy` 根据显式配置和查询条件构建 `RetrievalPlan`，不执行 I/O：

```python
class RetrievalRoutingPolicy:
    def __init__(self, config: InformationRetrievalConfig) -> None:
        self._config = config

    def create_plan(self, query: RetrievalQuery) -> RetrievalPlan:
        routes: list[RetrievalRoute] = []

        for source_name, source_config in self._config.sources.items():
            if not source_config.enabled:
                continue

            if not query.text and source_name in {"keyword", "vector"}:
                continue

            routes.append(
                RetrievalRoute(
                    source=source_name,
                    limit=source_config.limit,
                    timeout_seconds=source_config.timeout_seconds,
                    weight=source_config.weight,
                    required=source_config.required,
                )
            )

        return RetrievalPlan(query=query, routes=tuple(routes))
```

路由表由 Composition Root 明确配置。禁止通过字符串模式、目录扫描或反射自动启用源。

建议源语义：

| source | 数据边界 | 典型 required 设置 |
|---|---|---:|
| `keyword` | 文档和结构化文本关键词匹配 | false |
| `vector` | 文档和长期记忆语义相似度 | false |
| `preference` | 当前 actor 的已确认偏好 | true 或 false，按产品语义决定 |
| `history` | 当前 session/actor 的相关历史事件 | false |
| `long_term_memory` | 当前 actor 的长期记忆 | false |

`current_preferences` 若已由 `StateLoadPhase` 确定性加载，不应在本 Phase 重复查询。只有“按当前输入相关性筛选偏好”才属于 Preference Retriever。

---

## 10. 并发执行模型

### 10.1 并发原则

- 不做顶层 Phase 并行；只在 `InformationRetrievalPhase` 内并发多个独立检索源。
- 使用 `asyncio.TaskGroup` 管理任务生命周期。
- 每个任务内部捕获普通异常并转换为 `SourceOutcome`，避免一个可选源失败导致 TaskGroup 取消全部源。
- 使用 `asyncio.Semaphore` 限制并发数。
- 使用单源 timeout 和 Phase 总 timeout 双层保护。
- 取消信号不捕获、不降级。

### 10.2 内部 Outcome

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    route: RetrievalRoute
    batch: RetrievalBatch | None
    stats: RetrievalSourceStats
    failure: RetrievalSourceFailure | None
```

### 10.3 单源执行

```python
import asyncio
from time import perf_counter


async def _execute_source(
    self,
    *,
    retriever: RetrieverPort,
    route: RetrievalRoute,
    query: RetrievalQuery,
    semaphore: asyncio.Semaphore,
) -> SourceOutcome:
    started = perf_counter()

    try:
        async with semaphore:
            async with asyncio.timeout(route.timeout_seconds):
                batch = await retriever.retrieve(
                    query=query,
                    limit=route.limit,
                )

        self._validator.validate_batch(
            retriever_name=retriever.name,
            route=route,
            batch=batch,
        )

        accepted = await self._access_filter.filter(
            access=query.access,
            items=batch.items,
        )

        prepared = self._normalizer.normalize_batch(
            batch=RetrievalBatch(
                source=batch.source,
                items=tuple(accepted),
                partial=batch.partial,
            ),
            max_content_chars=self._config.max_content_chars,
        )

        duration_ms = int((perf_counter() - started) * 1000)
        return SourceOutcome(
            route=route,
            batch=prepared,
            stats=RetrievalSourceStats(
                source=route.source,
                duration_ms=duration_ms,
                received_count=len(batch.items),
                accepted_count=len(prepared.items),
                rejected_count=len(batch.items) - len(prepared.items),
            ),
            failure=None,
        )

    except TimeoutError:
        duration_ms = int((perf_counter() - started) * 1000)
        return SourceOutcome(
            route=route,
            batch=None,
            stats=RetrievalSourceStats(
                source=route.source,
                duration_ms=duration_ms,
                received_count=0,
                accepted_count=0,
                rejected_count=0,
                timed_out=True,
            ),
            failure=RetrievalSourceFailure(
                source=route.source,
                kind=RetrievalFailureKind.TIMEOUT,
                error_code="RETRIEVAL_SOURCE_TIMEOUT",
                safe_message=f"Retrieval source '{route.source}' timed out",
                retryable=True,
                duration_ms=duration_ms,
            ),
        )

    except RetrievalResultValidationError as exc:
        duration_ms = int((perf_counter() - started) * 1000)
        return SourceOutcome(
            route=route,
            batch=None,
            stats=RetrievalSourceStats(
                source=route.source,
                duration_ms=duration_ms,
                received_count=0,
                accepted_count=0,
                rejected_count=0,
            ),
            failure=RetrievalSourceFailure(
                source=route.source,
                kind=RetrievalFailureKind.INVALID_RESPONSE,
                error_code="RETRIEVAL_SOURCE_INVALID_RESPONSE",
                safe_message=f"Retrieval source '{route.source}' returned invalid data",
                retryable=False,
                duration_ms=duration_ms,
            ),
        )

    except RetrievalAdapterError as exc:
        duration_ms = int((perf_counter() - started) * 1000)
        return SourceOutcome(
            route=route,
            batch=None,
            stats=RetrievalSourceStats(
                source=route.source,
                duration_ms=duration_ms,
                received_count=0,
                accepted_count=0,
                rejected_count=0,
            ),
            failure=RetrievalSourceFailure(
                source=route.source,
                kind=exc.kind,
                error_code=exc.code,
                safe_message=exc.safe_message,
                retryable=exc.retryable,
                duration_ms=duration_ms,
            ),
        )

    except Exception:
        # 记录完整异常到内部日志；对外只暴露安全错误。
        self._logger.exception(
            "Unexpected retrieval source failure",
            extra={"source": route.source},
        )
        duration_ms = int((perf_counter() - started) * 1000)
        return SourceOutcome(
            route=route,
            batch=None,
            stats=RetrievalSourceStats(
                source=route.source,
                duration_ms=duration_ms,
                received_count=0,
                accepted_count=0,
                rejected_count=0,
            ),
            failure=RetrievalSourceFailure(
                source=route.source,
                kind=RetrievalFailureKind.INTERNAL,
                error_code="RETRIEVAL_SOURCE_INTERNAL_ERROR",
                safe_message=f"Retrieval source '{route.source}' failed",
                retryable=False,
                duration_ms=duration_ms,
            ),
        )
```

注意：此处只捕获 `Exception`，不得捕获 `BaseException`，因此取消信号可以正常传播。

### 10.4 多源执行

```python
async def _execute_routes(
    self,
    plan: RetrievalPlan,
) -> list[SourceOutcome]:
    semaphore = asyncio.Semaphore(self._config.max_concurrency)
    tasks: dict[str, asyncio.Task[SourceOutcome]] = {}

    async with asyncio.timeout(self._config.phase_timeout_seconds):
        async with asyncio.TaskGroup() as task_group:
            for route in plan.routes:
                retriever = self._retrievers[route.source]
                tasks[route.source] = task_group.create_task(
                    self._execute_source(
                        retriever=retriever,
                        route=route,
                        query=plan.query,
                        semaphore=semaphore,
                    ),
                    name=f"retrieval:{route.source}",
                )

    return [tasks[route.source].result() for route in plan.routes]
```

Phase 总 timeout 到期时应抛 `RetrievalPhaseTimeoutError`，而不是把未完成源当作普通空结果。

---

## 11. 结果校验

`RetrievalItemValidator` 在结果进入融合前执行。

必须拒绝：

- `batch.source != retriever.name`。
- 返回数量超过 route limit。
- 空 `item_id`。
- 空或仅空白 `content`。
- 非有限分数：`NaN`、`+inf`、`-inf`。
- Adapter 返回重复 `item_id` 且内容冲突。
- provenance 中的 source 与 Adapter 不一致。
- metadata 中包含明显不可序列化或禁止类型。

校验失败抛出纯领域异常 `RetrievalResultValidationError`，由单源执行包装为 `INVALID_RESPONSE` 失败。不得因为某个坏 Item 静默污染最终上下文。

对于内容长度超限：

- Normalizer 按明确上限截断。
- metadata 记录 `content_truncated=True`。
- 不在日志中记录被截断的正文。

---

## 12. 规范化和源内去重

### 12.1 内容规范化

- Unicode NFKC。
- 去除首尾空白。
- 保留段落结构。
- 将 CRLF 规范化为 LF。
- 移除 NUL 字符。
- 不修改事实含义。

### 12.2 `dedupe_key`

优先级：

1. Adapter 提供的稳定 canonical key。
2. 规范化 URI。
3. `kind + canonical entity id`。
4. `kind + SHA-256(normalized_content)`。

禁止使用 Python 内置 `hash()` 作为持久或跨进程去重键，因为其结果不稳定。

### 12.3 源内重复

同一源中 dedupe key 相同的项：

- 保留排名更高的项。
- 合并 provenance。
- metadata 冲突时保留主项 metadata，并把可审计来源放进 provenance metadata。

---

## 13. 跨源融合

### 13.1 Weighted RRF

对某个唯一项 `d`，融合分数为：

```text
rrf(d) = Σ source_weight(source) / (rrf_k + rank(source, d))
```

其中：

- `rank` 从 1 开始。
- 未命中该项的源不贡献分数。
- `rrf_k` 使用配置值，默认 60。
- source weight 来自显式路由配置。

归一化：

```text
normalized_score(d) = rrf(d) / max_rrf_score
```

`max_rrf_score` 是所有启用 route 在 rank=1 时的理论最大值。最终强制 clamp 到 `[0.0, 1.0]`。

### 13.2 为什么不用原始分数直接求和

- BM25 分数没有固定上限。
- 向量相似度可能是 `[-1, 1]`、`[0, 1]` 或距离值。
- 偏好置信度与文档相关性含义不同。
- 时间衰减和业务优先级不是同一度量。

RRF 使用排名而不是绝对分数，能稳定融合异构检索源。

### 13.3 合并规则

相同 `dedupe_key` 的多源结果：

- `item_id`：选择主结果的稳定 ID。
- `kind`：必须一致；不一致时按配置的 kind 优先级选主项，并记录冲突诊断。
- `content`：选择排名最高或内容更完整的主项，不拼接重复正文。
- `source`：主来源。
- `provenance`：合并全部来源并按 source、rank 稳定排序。
- `score`：RRF 归一化分数。

### 13.4 确定性实现骨架

```python
from collections import defaultdict
from dataclasses import replace


class WeightedReciprocalRankFusion:
    def __init__(self, *, rrf_k: int) -> None:
        self._rrf_k = rrf_k

    def merge(
        self,
        *,
        batches: Sequence[RetrievalBatch],
        routes: Sequence[RetrievalRoute],
    ) -> list[RetrievedItem]:
        route_by_source = {route.source: route for route in routes}
        score_by_key: dict[str, float] = defaultdict(float)
        items_by_key: dict[str, list[tuple[int, RetrievedItem]]] = defaultdict(list)

        for batch in batches:
            route = route_by_source[batch.source]
            for rank, item in enumerate(batch.items, start=1):
                key = self._require_dedupe_key(item)
                score_by_key[key] += route.weight / (self._rrf_k + rank)
                items_by_key[key].append((rank, item))

        max_score = sum(
            route.weight / (self._rrf_k + 1)
            for route in routes
        ) or 1.0

        merged: list[RetrievedItem] = []
        for key, ranked_items in items_by_key.items():
            ranked_items.sort(
                key=lambda pair: (
                    pair[0],
                    pair[1].source,
                    pair[1].item_id,
                )
            )
            primary = ranked_items[0][1]
            provenance = self._merge_provenance(ranked_items)
            score = min(max(score_by_key[key] / max_score, 0.0), 1.0)

            merged.append(
                replace(
                    primary,
                    score=score,
                    dedupe_key=key,
                    provenance=provenance,
                )
            )

        merged.sort(
            key=lambda item: (
                -item.score,
                item.kind.value,
                item.source,
                item.item_id,
            )
        )
        return merged
```

---

## 14. Rerank

### 14.1 输入范围

只将融合后前 `rerank_candidate_limit` 项发送给 Reranker，避免无边界成本和延迟。

### 14.2 执行顺序

```text
Fusion
  ↓
截取 rerank_candidate_limit
  ↓
Reranker
  ↓
再次 ACL 校验
  ↓
结果契约校验
  ↓
Diversity / Quota Selection
```

### 14.3 降级规则

- `reranker_fail_open=True`：Reranker 超时或失败时，保留 Fusion 排序并标记 `reranker_degraded=True`。
- `reranker_fail_open=False`：抛 `RetrievalRerankError`。
- Reranker 返回非法项、重复项、输入集合之外的项或非法分数，视为失败。
- 取消信号始终传播，不降级。

### 14.4 分数规则

Reranker 返回统一 `[0.0, 1.0]` 分数。若 Reranker 只返回顺序、不返回可靠分数，可按稳定排名映射：

```text
score(rank) = 1 - (rank - 1) / max(item_count - 1, 1)
```

该映射必须由 Reranker Adapter 完成，Phase 不猜测供应商分数语义。

---

## 15. 多样性与最终裁剪

最终结果不能被单一来源或单一 kind 完全占满。`RetrievalSelector` 执行确定性配额裁剪：

1. 按当前排序遍历。
2. 超过 `max_per_kind` 的项跳过。
3. 超过 `max_per_source` 的项跳过。
4. 达到 `final_limit` 后停止。
5. 若严格配额导致结果明显不足，执行第二遍放宽配额，但仍不得重复。

实现骨架：

```python
from collections import Counter
from typing import Sequence


class RetrievalSelector:
    def __init__(
        self,
        *,
        final_limit: int,
        max_per_kind: int,
        max_per_source: int,
    ) -> None:
        self._final_limit = final_limit
        self._max_per_kind = max_per_kind
        self._max_per_source = max_per_source

    def select(self, items: Sequence[RetrievedItem]) -> list[RetrievedItem]:
        selected: list[RetrievedItem] = []
        selected_ids: set[str] = set()
        kind_counts: Counter[RetrievedItemKind] = Counter()
        source_counts: Counter[str] = Counter()

        for item in items:
            if len(selected) >= self._final_limit:
                break
            if item.item_id in selected_ids:
                continue
            if kind_counts[item.kind] >= self._max_per_kind:
                continue
            if source_counts[item.source] >= self._max_per_source:
                continue

            selected.append(item)
            selected_ids.add(item.item_id)
            kind_counts[item.kind] += 1
            source_counts[item.source] += 1

        if len(selected) < self._final_limit:
            for item in items:
                if len(selected) >= self._final_limit:
                    break
                if item.item_id in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(item.item_id)

        return selected
```

---

## 16. 错误模型

在 `runtime/errors.py` 增加：

```python
class RetrievalError(RuntimeAgentError):
    code = "RETRIEVAL_ERROR"
    retryable = True


class InvalidRetrievalContextError(RetrievalError):
    code = "INVALID_RETRIEVAL_CONTEXT"
    retryable = False


class DuplicateRetrieverNameError(RetrievalError):
    code = "DUPLICATE_RETRIEVER_NAME"
    retryable = False


class RetrievalConfigurationError(RetrievalError):
    code = "RETRIEVAL_CONFIGURATION_ERROR"
    retryable = False


class RetrievalPhaseTimeoutError(RetrievalError):
    code = "RETRIEVAL_PHASE_TIMEOUT"
    retryable = True


class RequiredRetrievalSourceError(RetrievalError):
    code = "REQUIRED_RETRIEVAL_SOURCE_FAILED"
    retryable = True


class AllRetrievalSourcesFailedError(RetrievalError):
    code = "ALL_RETRIEVAL_SOURCES_FAILED"
    retryable = True


class RetrievalRerankError(RetrievalError):
    code = "RETRIEVAL_RERANK_ERROR"
    retryable = True
```

基础设施侧定义不泄漏供应商类型的 Adapter Error：

```python
class RetrievalResultValidationError(Exception):
    """Retriever 或 Reranker 返回值违反稳定契约。"""


class RetrievalAdapterError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        kind: RetrievalFailureKind,
        safe_message: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.safe_message = safe_message
        self.retryable = retryable
```

失败决策表：

| 情况 | Phase 行为 |
|---|---|
| 无路由 | 正常完成，空结果 |
| 某可选源无结果 | 正常完成 |
| 某可选源异常/超时 | 降级，记录 failure |
| 某必要源异常/超时 | 抛 `RequiredRetrievalSourceError` |
| 所有执行源失败 | 抛 `AllRetrievalSourcesFailedError` |
| 至少一个源成功但最终为空 | 正常完成，状态 `EMPTY` |
| Phase 总超时 | 抛 `RetrievalPhaseTimeoutError` |
| Reranker 失败且 fail-open | 使用 Fusion 结果，状态 `DEGRADED` |
| Reranker 失败且 fail-closed | 抛 `RetrievalRerankError` |
| Task 被取消 | 原样传播 `CancelledError` |

---

## 17. Phase 完整执行流程

```text
1. 校验 TurnContext 前置条件
2. 构建 RetrievalQuery
3. 生成 RetrievalPlan
4. 无路由时写入空结果与 EMPTY diagnostics，结束
5. 发出 RETRIEVAL_STARTED（不含正文）
6. 在 Phase 总 timeout 内受控并发执行所有 route
7. 对每个源：
   7.1 单源 timeout
   7.2 Adapter 调用
   7.3 批次契约校验
   7.4 ACL 防御性过滤
   7.5 内容规范化
   7.6 源内去重
8. 评估 required source 和 all-failed 规则
9. 将成功批次传给 Weighted RRF
10. 融合后再次 ACL 过滤
11. 截取 rerank candidate 集合
12. 执行 Reranker；按配置降级
13. 校验 Reranker 输出
14. 再次 ACL 过滤
15. 执行多样性与配额裁剪
16. 校验最终不变量
17. 写入 ctx.retrieved_items
18. 写入 ctx.retrieval_diagnostics
19. 发出 RETRIEVAL_COMPLETED（只含计数、耗时和降级标记）
```

---

## 18. `InformationRetrievalPhase` 实现骨架

```python
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from time import perf_counter

from cogito_agent.domain.retrieval import (
    RetrievalBatch,
    RetrievalCompletionStatus,
    RetrievalDiagnostics,
    RetrievalPlan,
    RetrievedItem,
)
from cogito_agent.ports.retrieval import (
    RetrievalAccessFilterPort,
    RetrievalFusionPort,
    RetrievalRerankerPort,
    RetrieverPort,
)
from cogito_agent.retrieval.diagnostics import RetrievalDiagnosticsBuilder
from cogito_agent.retrieval.normalization import RetrievalNormalizer
from cogito_agent.retrieval.query_builder import RetrievalQueryBuilder
from cogito_agent.retrieval.routing import RetrievalRoutingPolicy
from cogito_agent.retrieval.selection import RetrievalSelector
from cogito_agent.retrieval.validation import RetrievalItemValidator
from cogito_agent.runtime.context import TurnContext
from cogito_agent.runtime.errors import (
    AllRetrievalSourcesFailedError,
    DuplicateRetrieverNameError,
    RequiredRetrievalSourceError,
    RetrievalPhaseTimeoutError,
    RetrievalRerankError,
)
from cogito_agent.runtime.phase import BasePhase


class InformationRetrievalPhase(BasePhase):
    name = "information_retrieval"

    def __init__(
        self,
        *,
        retrievers: Sequence[RetrieverPort],
        query_builder: RetrievalQueryBuilder,
        routing_policy: RetrievalRoutingPolicy,
        validator: RetrievalItemValidator,
        access_filter: RetrievalAccessFilterPort,
        normalizer: RetrievalNormalizer,
        fusion: RetrievalFusionPort,
        reranker: RetrievalRerankerPort,
        selector: RetrievalSelector,
        diagnostics_builder: RetrievalDiagnosticsBuilder,
        config: InformationRetrievalConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self._retrievers = self._index_retrievers(retrievers)
        self._query_builder = query_builder
        self._routing_policy = routing_policy
        self._validator = validator
        self._access_filter = access_filter
        self._normalizer = normalizer
        self._fusion = fusion
        self._reranker = reranker
        self._selector = selector
        self._diagnostics_builder = diagnostics_builder
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._validate_configuration()

    @staticmethod
    def _index_retrievers(
        retrievers: Sequence[RetrieverPort],
    ) -> dict[str, RetrieverPort]:
        indexed: dict[str, RetrieverPort] = {}
        for retriever in retrievers:
            name = retriever.name.strip()
            if not name:
                raise DuplicateRetrieverNameError(
                    "Retriever name must not be empty"
                )
            if name in indexed:
                raise DuplicateRetrieverNameError(
                    f"Duplicate retriever name: {name}"
                )
            indexed[name] = retriever
        return indexed

    async def execute(self, ctx: TurnContext) -> None:
        started = perf_counter()
        query = self._query_builder.build(ctx)
        plan = self._routing_policy.create_plan(query)

        if not plan.routes:
            ctx.retrieved_items = []
            ctx.retrieval_diagnostics = self._diagnostics_builder.empty(
                total_duration_ms=self._elapsed_ms(started),
            )
            await self._emit_retrieval_completed(ctx)
            return

        await self._emit_retrieval_started(ctx, plan)

        try:
            outcomes = await self._execute_routes(plan)
        except TimeoutError as exc:
            raise RetrievalPhaseTimeoutError(
                "Information retrieval phase timed out",
                safe_message="Information retrieval timed out",
            ) from exc

        failures = [
            outcome.failure
            for outcome in outcomes
            if outcome.failure is not None
        ]
        required_failures = [
            outcome
            for outcome in outcomes
            if outcome.route.required and outcome.failure is not None
        ]

        if required_failures:
            names = ", ".join(
                outcome.route.source for outcome in required_failures
            )
            raise RequiredRetrievalSourceError(
                f"Required retrieval sources failed: {names}",
                safe_message="A required retrieval source is unavailable",
            )

        successful_batches = [
            outcome.batch
            for outcome in outcomes
            if outcome.batch is not None
        ]

        if not successful_batches:
            raise AllRetrievalSourcesFailedError(
                "All configured retrieval sources failed",
                safe_message="Information retrieval is temporarily unavailable",
            )

        pre_fusion_count = sum(
            len(batch.items) for batch in successful_batches
        )

        fused = self._fusion.merge(
            batches=successful_batches,
            routes=plan.routes,
        )

        fused = await self._access_filter.filter(
            access=query.access,
            items=fused,
        )
        post_fusion_count = len(fused)

        reranker_used = bool(fused)
        reranker_degraded = False

        if fused:
            candidates = fused[: self._config.rerank_candidate_limit]
            try:
                async with asyncio.timeout(
                    self._config.reranker_timeout_seconds
                ):
                    reranked = await self._reranker.rerank(
                        query=query,
                        items=candidates,
                        limit=self._config.rerank_candidate_limit,
                    )
                self._validator.validate_reranked(
                    inputs=candidates,
                    outputs=reranked,
                )
            except Exception as exc:
                if not self._config.reranker_fail_open:
                    raise RetrievalRerankError(
                        "Retrieval reranker failed",
                        safe_message="Information reranking failed",
                    ) from exc
                self._logger.exception("Retrieval reranker degraded")
                reranked = candidates
                reranker_degraded = True
        else:
            reranked = []

        reranked = await self._access_filter.filter(
            access=query.access,
            items=reranked,
        )

        final_items = self._selector.select(reranked)
        self._validator.validate_final(final_items)

        status = self._resolve_status(
            final_items=final_items,
            failures=failures,
            reranker_degraded=reranker_degraded,
        )

        ctx.retrieved_items = final_items
        ctx.retrieval_diagnostics = self._diagnostics_builder.build(
            status=status,
            total_duration_ms=self._elapsed_ms(started),
            plan=plan,
            outcomes=outcomes,
            pre_fusion_count=pre_fusion_count,
            post_fusion_count=post_fusion_count,
            final_count=len(final_items),
            reranker_used=reranker_used,
            reranker_degraded=reranker_degraded,
        )
        await self._emit_retrieval_completed(ctx)

    async def _emit_retrieval_started(
        self,
        ctx: TurnContext,
        plan: RetrievalPlan,
    ) -> None:
        if ctx.event_emitter is None:
            return
        await ctx.event_emitter.emit(
            event_type=AgentEventType.RETRIEVAL_STARTED,
            phase=self.name,
            data={
                "selected_sources": [route.source for route in plan.routes],
                "requested_limit": plan.query.limit,
            },
        )

    async def _emit_retrieval_completed(self, ctx: TurnContext) -> None:
        diagnostics = ctx.retrieval_diagnostics
        if ctx.event_emitter is None or diagnostics is None:
            return
        await ctx.event_emitter.emit(
            event_type=AgentEventType.RETRIEVAL_COMPLETED,
            phase=self.name,
            data={
                "status": diagnostics.status.value,
                "duration_ms": diagnostics.total_duration_ms,
                "successful_sources": list(diagnostics.successful_sources),
                "failed_sources": [
                    failure.source for failure in diagnostics.failures
                ],
                "pre_fusion_count": diagnostics.pre_fusion_count,
                "post_fusion_count": diagnostics.post_fusion_count,
                "final_count": diagnostics.final_count,
                "reranker_used": diagnostics.reranker_used,
                "reranker_degraded": diagnostics.reranker_degraded,
            },
        )

    @staticmethod
    def _resolve_status(
        *,
        final_items: Sequence[RetrievedItem],
        failures: Sequence[object],
        reranker_degraded: bool,
    ) -> RetrievalCompletionStatus:
        if failures or reranker_degraded:
            return RetrievalCompletionStatus.DEGRADED
        if not final_items:
            return RetrievalCompletionStatus.EMPTY
        return RetrievalCompletionStatus.COMPLETED

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((perf_counter() - started) * 1000)
```

### 18.1 取消处理修正

上面 Reranker 示例中的 `except Exception` 不会捕获 Python 3.12 的 `asyncio.CancelledError`，因此取消可以传播。实现中不得改为 `except BaseException`。

### 18.2 原子写入 Context

`ctx.retrieved_items` 和 `ctx.retrieval_diagnostics` 只在最终校验通过后写入。不要在处理中逐步修改 Context，以避免 Phase 失败后留下半成品状态。

---

## 19. 事件和可观测性

### 19.1 事件

Phase 应发出：

```text
RETRIEVAL_STARTED
RETRIEVAL_COMPLETED
```

失败由 Kernel 的通用 `PHASE_FAILED` 和 `TURN_FAILED` 处理；可选源失败放入完成事件的降级信息，不单独暴露异常正文。

### 19.2 事件数据

`RETRIEVAL_STARTED.data`：

```json
{
  "selected_sources": ["keyword", "vector", "preference"],
  "requested_limit": 20
}
```

`RETRIEVAL_COMPLETED.data`：

```json
{
  "status": "degraded",
  "duration_ms": 184,
  "successful_sources": ["keyword", "preference"],
  "failed_sources": ["vector"],
  "pre_fusion_count": 34,
  "post_fusion_count": 21,
  "final_count": 15,
  "reranker_used": true,
  "reranker_degraded": false
}
```

不得放入事件：

- Query 全文。
- 检索结果正文。
- Embedding。
- 系统 Prompt。
- 用户私有偏好内容。
- 数据库查询语句。
- 异常堆栈。
- Token、密钥、连接字符串。

### 19.3 Metrics

建议指标：

```text
agent_retrieval_phase_duration_ms
agent_retrieval_source_duration_ms{source}
agent_retrieval_source_requests_total{source,status}
agent_retrieval_source_timeouts_total{source}
agent_retrieval_items_received_total{source}
agent_retrieval_items_rejected_total{source,reason}
agent_retrieval_items_final_total
agent_retrieval_degraded_total{reason}
agent_retrieval_reranker_duration_ms
agent_retrieval_reranker_failures_total
```

禁止把 `actor_id`、`session_id`、`request_id` 作为高基数 Metrics label。它们只能进入 Trace/日志结构化字段，并按隐私策略处理。

### 19.4 Trace Span

建议 Span：

```text
information_retrieval
├── retrieval.keyword
├── retrieval.vector
├── retrieval.preference
├── retrieval.history
├── retrieval.long_term_memory
├── retrieval.fusion
├── retrieval.rerank
└── retrieval.selection
```

Span 属性只保存计数、耗时、状态和源名称，不保存内容正文。

---

## 20. Adapter 实现要求

### 20.1 Keyword Retriever

职责：

- 使用关键词/BM25/全文索引检索。
- 根据 `RetrievalFilters` 下推时间、标签、kind、namespace 过滤。
- 根据 `RetrievalAccessContext` 下推 ACL。
- 返回源内排序结果。
- 将搜索引擎 hit 映射为领域 `RetrievedItem`。

不得：

- 将搜索引擎原生对象放入 metadata。
- 在 Adapter 内执行跨源融合。
- 直接写 `TurnContext`。

### 20.2 Vector Retriever

职责：

- 对 query text 生成或读取 query embedding。
- 在 actor/tenant/namespace 允许范围内做向量搜索。
- 将距离或相似度转成源内 `[0.0, 1.0]` 分数。
- 返回稳定 source rank。

要求：

- Embedding 生成失败映射为 Adapter Error。
- 不在日志或事件记录完整向量。
- 区分“距离越小越好”和“相似度越大越好”。
- 维度不匹配视为不可重试配置错误。

### 20.3 Preference Retriever

职责：

- 只检索当前 actor 的已确认或允许使用的偏好。
- 根据当前 query 做相关性筛选。
- 区分 confirmed、tentative、deleted 状态。
- 默认不返回已删除或低置信度 tentative 偏好。

### 20.4 History Retriever

职责：

- 限定当前 actor/session 或明确允许的跨 session 范围。
- 对历史消息、事件或摘要做相关性检索。
- 使用时间衰减时，把衰减限制在 Adapter 内源内排序，不和其他源 raw score 直接相加。

### 20.5 Long-Term Memory Retriever

职责：

- 只检索已持久化且可用的长期记忆。
- 遵守记忆状态、有效期、删除标记和隐私范围。
- 对冲突或过期记忆通过 metadata 标识，供后续策略过滤。

---

## 21. Composition Root

```python
def build_information_retrieval_phase(
    *,
    keyword_retriever: RetrieverPort,
    vector_retriever: RetrieverPort,
    preference_retriever: RetrieverPort,
    history_retriever: RetrieverPort,
    memory_retriever: RetrieverPort,
    access_filter: RetrievalAccessFilterPort,
    reranker: RetrievalRerankerPort,
    config: InformationRetrievalConfig,
) -> InformationRetrievalPhase:
    query_builder = RetrievalQueryBuilder(
        default_limit=config.final_limit,
    )
    routing_policy = RetrievalRoutingPolicy(config)
    validator = RetrievalItemValidator()
    normalizer = RetrievalNormalizer()
    fusion = WeightedReciprocalRankFusion(rrf_k=config.rrf_k)
    selector = RetrievalSelector(
        final_limit=config.final_limit,
        max_per_kind=config.max_per_kind,
        max_per_source=config.max_per_source,
    )
    diagnostics_builder = RetrievalDiagnosticsBuilder()

    return InformationRetrievalPhase(
        retrievers=[
            keyword_retriever,
            vector_retriever,
            preference_retriever,
            history_retriever,
            memory_retriever,
        ],
        query_builder=query_builder,
        routing_policy=routing_policy,
        validator=validator,
        access_filter=access_filter,
        normalizer=normalizer,
        fusion=fusion,
        reranker=reranker,
        selector=selector,
        diagnostics_builder=diagnostics_builder,
        config=config,
    )
```

在 Runtime Factory 中显式加入原有顺序：

```python
phases: list[RuntimePhase] = [
    TurnInitPhase(...),
    StateLoadPhase(...),
    build_information_retrieval_phase(...),
    ContextAssemblyPhase(...),
    AgentLoopPhase(...),
    KnowledgeExtractionPhase(...),
    PersistencePhase(...),
    TurnFinalizePhase(...),
]
```

Kernel 不需要任何改动。

---

## 22. 测试策略

### 22.1 Query Builder 单元测试

必须覆盖：

- Unicode 和空白规范化。
- actor/session/tenant 访问范围构建。
- 空文本。
- 超长文本。
- locale 解析。
- 不把近期历史全文拼进 query。
- `turn_id` 缺失时失败。
- 用户 metadata 不能提升 ACL。

### 22.2 Routing 单元测试

- 启用源生成 route。
- 禁用源不生成 route。
- 空文本跳过 keyword/vector。
- source limit、timeout、weight、required 正确传递。
- 路由顺序稳定。
- 配置引用不存在的 Retriever 时启动失败。

### 22.3 并发与 timeout 测试

- 多个 Retriever 实际并发启动。
- 并发数不超过 `max_concurrency`。
- 单源超时被记录为可选失败。
- Phase 总超时抛稳定错误。
- Task 取消传播。
- 一个可选源失败不取消成功源。
- 必要源失败终止 Phase。
- 所有源失败抛稳定错误。

### 22.4 ACL 测试

- Adapter 返回越权项时被 Phase 过滤。
- 融合后越权项不出现。
- Reranker 尝试返回越权项时被过滤或拒绝。
- actor A 无法检索 actor B 的偏好、历史和记忆。
- namespace/tenant 隔离。

### 22.5 Validator 测试

- source 不一致。
- 超过 route limit。
- 空 item ID。
- 空 content。
- NaN/Infinity score。
- 重复 item ID 内容冲突。
- 非法 metadata。
- Reranker 返回输入集合外项。
- Reranker 返回重复项。

### 22.6 Fusion 测试

- 单源排名保持。
- 多源命中提高 RRF 分数。
- source weight 生效。
- 不直接混合 raw score。
- dedupe key 合并。
- provenance 合并完整。
- 分数范围 `[0.0, 1.0]`。
- 相同输入输出顺序确定。
- tie-break 生效。

### 22.7 Reranker 测试

- 正常重排。
- 超时且 fail-open。
- 异常且 fail-open。
- 异常且 fail-closed。
- 非法输出触发降级或错误。
- 最多接收 `rerank_candidate_limit` 个候选。

### 22.8 Selector 测试

- `final_limit`。
- `max_per_kind`。
- `max_per_source`。
- 第二遍补足结果。
- 不产生重复。
- 顺序稳定。

### 22.9 Phase 集成测试

使用 Fake Retriever 和 Recording Access Filter，验证完整顺序：

```text
query build
→ route
→ concurrent retrieve
→ validate
→ access filter
→ normalize
→ fusion
→ access filter
→ rerank
→ validate
→ access filter
→ select
→ context commit
```

断言：

- `ctx.retrieved_items` 只在成功结尾写入。
- 失败时不留下半成品结果。
- diagnostics 中计数正确。
- 空结果不是错误。
- 降级结果状态为 `DEGRADED`。

### 22.10 Retriever Contract Test

所有 Adapter 复用同一套契约测试：

```python
class RetrieverContract:
    async def test_name_is_stable_and_non_empty(self, retriever): ...
    async def test_batch_source_matches_name(self, retriever): ...
    async def test_does_not_exceed_limit(self, retriever): ...
    async def test_empty_result_is_valid(self, retriever): ...
    async def test_items_are_ranked(self, retriever): ...
    async def test_cancellation_propagates(self, retriever): ...
    async def test_access_scope_is_enforced(self, retriever): ...
```

### 22.11 架构测试

确保以下目录不得导入：

```text
redis
nats
kafka
rabbitmq
telegram
discord
fastapi
starlette
sqlalchemy
具体向量数据库 SDK
具体搜索引擎 SDK
```

适用目录：

```text
cogito_agent/domain/
cogito_agent/ports/
cogito_agent/retrieval/
cogito_agent/runtime/
```

具体 SDK 只允许出现在 `infrastructure/retrieval/`。

---

## 23. 性能边界

### 23.1 有界数量

- 每个源有独立 `limit`。
- Fusion 候选数量受所有 source limit 之和约束。
- Reranker 只处理 `rerank_candidate_limit`。
- 最终结果最多 `final_limit`。
- 每项 content 有 `max_content_chars`。

### 23.2 有界时间

- 单源 timeout。
- Reranker timeout。
- Phase 总 timeout。
- Kernel 仍可提供 Turn 总 timeout。

### 23.3 有界并发

- Phase 内使用 Semaphore。
- Adapter 内部若继续并发，必须另有明确上限。
- 不在模块 import 时创建连接、任务或线程池。

### 23.4 缓存边界

缓存属于 Adapter 或独立基础设施组件，不直接写进 Phase。缓存 key 必须包含：

- 检索源版本化语义标识。
- actor/tenant/namespace 访问范围。
- 规范化 query。
- filters。
- limit。

禁止跨 actor 复用私有检索结果缓存。

---

## 24. 安全与隐私要求

1. 所有私有检索必须绑定 `RetrievalAccessContext`。
2. ACL 必须下推到数据源，Phase 过滤只是第二道防线。
3. 检索正文不进入普通事件和 Metrics。
4. 日志不得记录 Embedding、密钥、系统 Prompt 或完整私有文档。
5. metadata 必须经过允许类型检查和敏感键清理。
6. 结果 URI 若包含签名参数，进入 provenance 前必须脱敏或改为内部资源 ID。
7. 删除、过期、撤回或被策略禁止的记忆不得返回。
8. Reranker 若调用外部服务，必须遵守数据出境、隐私和脱敏策略。
9. 任何 ACL 不确定状态默认拒绝，不默认放行。
10. 错误事件只包含稳定 code 和 safe message。

---

## 25. 实现顺序

按以下顺序落地，避免组件互相反向依赖：

### 第一步：领域模型

实现：

- `RetrievalAccessContext`
- `RetrievalFilters`
- `RetrievalQuery`
- `RetrievalRoute`
- `RetrievalPlan`
- `RetrievalProvenance`
- `RetrievedItem`
- `RetrievalBatch`
- `RetrievalSourceFailure`
- `RetrievalSourceStats`
- `RetrievalDiagnostics`

并更新 `TurnContext`。

### 第二步：Port 与错误类型

实现：

- `RetrieverPort`
- `RetrievalAccessFilterPort`
- `RetrievalFusionPort`
- `RetrievalRerankerPort`
- Runtime Retrieval Error
- Adapter Error

### 第三步：纯组件

实现并单测：

- Query Builder
- Routing Policy
- Validator
- Normalizer
- Weighted RRF Fusion
- Selector
- Diagnostics Builder

### 第四步：Phase 编排

实现：

- Retriever 唯一性校验。
- 配置校验。
- 多源并发。
- timeout。
- 失败决策。
- Fusion。
- Rerank 降级。
- Context 原子提交。

### 第五步：Fake Adapter 和 Phase 测试

先使用 Fake Retriever 完成全部行为测试，不依赖真实数据库或搜索服务。

### 第六步：基础设施 Adapter

分别实现：

- Keyword Retriever Adapter。
- Vector Retriever Adapter。
- Preference Retriever Adapter。
- History Retriever Adapter。
- Long-Term Memory Retriever Adapter。

每个 Adapter 必须通过统一 Contract Test。

### 第七步：Composition Root 和集成测试

- 显式注入全部源和策略。
- 验证 Runtime Pipeline 顺序不变。
- 验证 Kernel 无需增加业务分支。
- 验证架构边界。

### 第八步：观测与生产约束

- Retrieval events。
- Metrics。
- Trace spans。
- 日志脱敏。
- timeout、并发、limit 的部署配置验证。

---

## 26. 验收标准

### 26.1 架构

- [ ] `InformationRetrievalPhase` 只依赖 Domain、Port 和纯检索组件。
- [ ] Phase 不依赖 Channel、MessageBus 或具体基础设施 SDK。
- [ ] 所有 Retriever 由 Composition Root 显式注入。
- [ ] 不存在模块扫描、动态 Registry 或 Service Locator。
- [ ] Kernel 不包含检索业务分支。

### 26.2 功能

- [ ] 能构建强类型 `RetrievalQuery`。
- [ ] 能显式路由五类检索源。
- [ ] 能受控并发执行多个源。
- [ ] 能处理单源 timeout、部分失败和必要源失败。
- [ ] 能校验 Adapter 返回契约。
- [ ] 能执行两阶段 ACL 防御过滤。
- [ ] 能执行稳定跨源去重。
- [ ] 能使用 Weighted RRF 融合异构结果。
- [ ] 能执行可降级 Rerank。
- [ ] 能执行来源和 kind 多样性裁剪。
- [ ] 最终结果分数全部在 `[0.0, 1.0]`。
- [ ] 最终结果顺序确定。
- [ ] 空结果是合法完成状态。
- [ ] 输出写入 `ctx.retrieved_items`。
- [ ] 诊断写入 `ctx.retrieval_diagnostics`。

### 26.3 可靠性

- [ ] `CancelledError` 原样传播。
- [ ] Phase 总 timeout 有稳定错误。
- [ ] 可选源失败不会中断成功源。
- [ ] 必要源失败不会被静默降级。
- [ ] 所有源失败不会伪造空成功。
- [ ] Reranker 失败按配置 fail-open 或 fail-closed。
- [ ] Context 不会留下半成品状态。

### 26.4 安全

- [ ] actor、session、tenant、namespace 隔离通过测试。
- [ ] Adapter 执行源端 ACL。
- [ ] Phase 执行防御性 ACL。
- [ ] 事件和 Metrics 不包含正文和敏感信息。
- [ ] metadata 不包含连接、凭证和供应商对象。
- [ ] 外部 Reranker 的数据发送符合隐私策略。

### 26.5 测试与质量

- [ ] Query、Routing、Validation、Fusion、Selector 单测全部通过。
- [ ] Phase 正常、空结果、降级、失败、超时、取消测试全部通过。
- [ ] 每个真实 Retriever 通过统一 Contract Test。
- [ ] 集成测试验证完整检索链路。
- [ ] 架构依赖边界测试通过。
- [ ] 类型检查、lint 和异步测试通过。

---

## 27. 最终边界总结

`InformationRetrievalPhase` 的最终职责边界是：

```text
已验证的 AgentRequest
+ StateLoadPhase 已加载的确定性状态
        │
        ▼
构建 RetrievalQuery / RetrievalPlan
        │
        ▼
显式、受控并发执行 Retriever Ports
        │
        ▼
校验 → ACL → 规范化 → 源内去重
        │
        ▼
Weighted RRF 跨源融合
        │
        ▼
Rerank → ACL → 多样性裁剪
        │
        ▼
TurnContext.retrieved_items
TurnContext.retrieval_diagnostics
```

后续 `ContextAssemblyPhase` 只消费检索结果，不重新检索；`KnowledgeExtractionPhase` 只生成新知识候选，不修改本轮检索；`PersistencePhase` 只负责事务性写入。该边界保证检索能力可替换、可测试、可降级、可审计，同时不破坏 RuntimeKernel、Channel 和 MessageBus 的解耦设计。
