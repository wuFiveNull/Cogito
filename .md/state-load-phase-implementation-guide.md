# StateLoadPhase 具体实现路径

> 适用项目：Cogito-Agent 初始运行时框架  
> 目标版本：Python 3.12+ / asyncio / Hexagonal Architecture  
> 文档状态：可直接进入编码阶段

---

## 1. 目标与结论

`StateLoadPhase` 位于 `TurnInitPhase` 之后、`InformationRetrievalPhase` 之前，其唯一职责是：

> 根据 `AgentRequest` 中明确、确定的标识符，读取本轮执行所需的确定性状态，并写入 `TurnContext`。

推荐的第一版实现路径如下：

1. 为 Session、消息历史、摘要、用户档案、用户设置和会话配置建立清晰的领域 DTO。
2. 为每类读取能力建立窄接口 Repository Port，不让 Phase 依赖数据库实现。
3. 在 `StateLoadPhase.execute()` 中按明确顺序执行只读加载。
4. 将加载结果一次性写入 `TurnContext` 对应强类型字段。
5. 对“资源不存在”和“存储访问失败”采用不同语义。
6. 不在本阶段执行向量检索、关键词检索、Prompt 组装、模型调用或持久化。
7. 第一版优先使用顺序加载；确认正确性后，再对彼此独立的读取做受控并发优化。

最终数据流：

```text
AgentRequest
    │
    │ session_id / actor_id
    ▼
StateLoadPhase
    ├── SessionRepositoryPort.get(session_id)
    ├── MessageRepositoryPort.list_recent(session_id, limit)
    ├── SummaryRepositoryPort.get(session_id)
    ├── UserProfileRepositoryPort.get(actor_id)
    ├── UserSettingsRepositoryPort.get(actor_id)
    └── SessionConfigRepositoryPort.get(session_id)
    │
    ▼
TurnContext
    ├── session
    ├── recent_messages
    ├── session_summary
    ├── user_profile
    ├── user_settings
    └── session_config
```

---

## 2. 职责边界

### 2.1 本阶段负责什么

| 状态 | 查询方式 | 是否属于 StateLoadPhase | 原因 |
|---|---|---:|---|
| Session | 按 `session_id` 精确读取 | 是 | 确定性状态 |
| 最近消息 | 按 `session_id` 和固定条数读取 | 是 | 确定性历史窗口 |
| Session Summary | 按 `session_id` 精确读取 | 是 | 确定性状态 |
| 用户基础档案 | 按 `actor_id` 精确读取 | 是 | 确定性状态 |
| 用户设置 | 按 `actor_id` 精确读取 | 是 | 确定性配置 |
| 会话级配置 | 按 `session_id` 精确读取 | 是 | 确定性配置 |
| 与当前输入相关的历史 | 关键词、向量或重排 | 否 | 属于相关性检索 |
| 长期记忆 | 语义相似度或检索策略 | 否 | 属于相关性检索 |
| 用户偏好候选 | 从本轮内容抽取 | 否 | 属于知识抽取 |
| Prompt / Model Messages | 按预算组装 | 否 | 属于 ContextAssemblyPhase |

### 2.2 “最近历史消息”与“相关历史消息”的区别

这是实现中最容易混淆的边界。

```text
最近历史消息：
    SELECT ... WHERE session_id = ? ORDER BY sequence DESC LIMIT ?
    → StateLoadPhase

相关历史消息：
    keyword_search(text) / vector_search(embedding) / rerank(...)
    → InformationRetrievalPhase
```

`StateLoadPhase` 的最近历史窗口是固定且可预测的；不根据当前输入内容计算相关性。

### 2.3 用户设置与用户偏好的区别

建议严格拆分：

```text
确定性用户设置：
- locale = "zh-CN"
- timezone = "Asia/Tokyo"
- response_style = "concise"
- tool_approval_mode = "manual"

可检索用户偏好：
- 用户偏爱川菜
- 用户不喜欢长篇解释
- 用户通常在周末出行
```

前者由 `StateLoadPhase` 读取；后者由 `InformationRetrievalPhase` 根据当前请求筛选相关项。

因此，第一版不要在 `StateLoadPhase` 中填充 `ctx.current_preferences`，除非该字段明确表示“全量、确定性偏好快照”。更推荐将其保留给检索阶段，或者后续重命名为 `retrieved_preferences`。

---

## 3. 执行契约

### 3.1 前置条件

进入 `StateLoadPhase` 时，`TurnInitPhase` 应已经完成以下工作：

```python
ctx.turn_id is not None
ctx.started_at is not None
ctx.status == TurnStatus.RUNNING
ctx.request.session_id != ""
ctx.request.actor_id != ""
```

`StateLoadPhase` 不负责生成 `turn_id`，也不应修复非法请求。

### 3.2 后置条件

成功完成后：

```python
ctx.session                  # SessionState | None
ctx.recent_messages          # list[ConversationMessage]
ctx.session_summary          # SessionSummary | None
ctx.user_profile             # UserProfile | None
ctx.user_settings            # UserSettings
ctx.session_config           # SessionConfig
```

必须保证：

- 列表字段不会是 `None`。
- 设置与配置字段具备明确默认值。
- 不修改 `ctx.retrieved_items`。
- 不修改 `ctx.model_messages`。
- 不修改 `ctx.output_text`。
- 不写入任何 Repository。

### 3.3 Phase 幂等性

在相同存储快照下，多次执行应得到等价结果：

```text
execute(ctx) + execute(ctx)
```

不应产生新消息、不应创建 Session、不应更新访问时间、不应提交事务。

若业务要求记录“最后访问时间”，应放到 `PersistencePhase`，而不是在读取阶段隐式写入。

---

## 4. 推荐领域模型

初始规格允许使用 `object` 占位，但实现 `StateLoadPhase` 时应优先替换掉它们。

建议新增：

```text
cogito_agent/domain/state.py
```

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class SessionLifecycle(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SessionState:
    session_id: str
    actor_id: str
    lifecycle: SessionLifecycle = SessionLifecycle.ACTIVE
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    message_id: str
    session_id: str
    actor_id: str | None
    role: str
    content: str
    sequence: int
    created_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    content: str
    version: int
    updated_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserProfile:
    actor_id: str
    display_name: str | None = None
    locale: str | None = None
    timezone: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserSettings:
    locale: str = "zh-CN"
    timezone: str = "UTC"
    response_style: str | None = None
    tool_approval_mode: str = "default"
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    history_limit: int = 20
    max_tool_rounds: int | None = None
    model_profile: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### 4.1 为什么设置和配置使用 DTO，而不是 `dict`

`dict[str, object]` 会导致以下问题：

- 拼写错误只能在运行时暴露。
- 下游无法知道字段是否存在。
- 默认值散落在多个 Phase。
- 类型检查无法帮助重构。
- 配置兼容策略无法集中管理。

建议让 `TurnContext` 直接保存领域 DTO；只有真正开放的附加信息放入 `metadata`。

---

## 5. TurnContext 调整

推荐将确定性状态区改为：

```python
from cogito_agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)


@dataclass(slots=True)
class TurnContext:
    request: AgentRequest

    # ... lifecycle fields omitted

    session: SessionState | None = None
    recent_messages: list[ConversationMessage] = field(default_factory=list)
    session_summary: SessionSummary | None = None
    user_profile: UserProfile | None = None
    user_settings: UserSettings = field(default_factory=UserSettings)
    session_config: SessionConfig = field(default_factory=SessionConfig)

    # ... remaining fields omitted
```

### 5.1 `max_tool_rounds` 的覆盖规则

若 `SessionConfig.max_tool_rounds` 不为空，可在本阶段应用配置：

```python
if session_config.max_tool_rounds is not None:
    ctx.max_tool_rounds = session_config.max_tool_rounds
```

应增加范围校验：

```python
1 <= max_tool_rounds <= 32
```

推荐优先级：

```text
请求级显式值（未来扩展）
    > SessionConfig
    > Runtime 默认值
```

第一版只有 SessionConfig 与 Runtime 默认值时，不要引入复杂配置合并框架。

---

## 6. Port 设计

### 6.1 不要过载现有 Repository

不推荐让 `SessionRepositoryPort.get()` 同时返回：

```text
Session + Summary + UserProfile + UserSettings + Config
```

这样会产生隐式聚合、难以独立测试，并迫使所有存储实现使用同一种数据布局。

建议保持窄接口：

```python
from __future__ import annotations

from typing import Protocol

from cogito_agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)


class SessionRepositoryPort(Protocol):
    async def get(self, session_id: str) -> SessionState | None:
        ...


class MessageRepositoryPort(Protocol):
    async def list_recent(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        ...


class SummaryRepositoryPort(Protocol):
    async def get(self, session_id: str) -> SessionSummary | None:
        ...


class UserProfileRepositoryPort(Protocol):
    async def get(self, actor_id: str) -> UserProfile | None:
        ...


class UserSettingsRepositoryPort(Protocol):
    async def get(self, actor_id: str) -> UserSettings | None:
        ...


class SessionConfigRepositoryPort(Protocol):
    async def get(self, session_id: str) -> SessionConfig | None:
        ...
```

### 6.2 Repository 返回值约定

| 方法 | 没有记录时 | 访问失败时 |
|---|---|---|
| `SessionRepositoryPort.get` | `None` | 抛存储异常 |
| `MessageRepositoryPort.list_recent` | `[]` | 抛存储异常 |
| `SummaryRepositoryPort.get` | `None` | 抛存储异常 |
| `UserProfileRepositoryPort.get` | `None` | 抛存储异常 |
| `UserSettingsRepositoryPort.get` | `None` | 抛存储异常 |
| `SessionConfigRepositoryPort.get` | `None` | 抛存储异常 |

Repository 不应把数据库异常转换成空结果，否则 Phase 无法区分“没有数据”和“存储不可用”。

---

## 7. 缺失数据策略

### 7.1 推荐默认策略

| 状态 | 缺失是否失败 | 推荐行为 |
|---|---:|---|
| Session | 否 | 保持 `None`，视为新会话或尚未持久化会话 |
| 最近消息 | 否 | 使用空列表 |
| Summary | 否 | 保持 `None` |
| UserProfile | 否 | 保持 `None` |
| UserSettings | 否 | 使用注入的默认设置 |
| SessionConfig | 否 | 使用注入的默认配置 |

这使第一轮新会话可以继续执行，同时保持 `StateLoadPhase` 只读。

### 7.2 Session 与 actor 一致性

如果 Session 存在，必须验证其归属：

```python
if session is not None and session.actor_id != ctx.request.actor_id:
    raise SessionActorMismatchError(
        session_id=ctx.request.session_id,
        actor_id=ctx.request.actor_id,
    )
```

这是授权边界，不应依赖下游自行发现。

对外错误信息应安全，例如：

```text
当前会话不可访问
```

不要泄漏 Session 实际所属用户。

### 7.3 历史消息排序

Repository 的接口应定义清楚输出顺序。推荐：

```text
按 sequence 升序返回，即从旧到新。
```

即使数据库为了性能先取最新 N 条，也应在 Adapter 内转换为旧到新：

```sql
SELECT *
FROM (
    SELECT *
    FROM messages
    WHERE session_id = :session_id
    ORDER BY sequence DESC
    LIMIT :limit
) AS recent
ORDER BY sequence ASC;
```

Phase 不应猜测或重复排序存储结果；排序契约应由 Port 文档固定。

---

## 8. StateLoadPhase 构造函数

推荐实现文件：

```text
cogito_agent/runtime/phases/state_load.py
```

```python
from __future__ import annotations

from dataclasses import dataclass

from cogito_agent.domain.state import SessionConfig, UserSettings
from cogito_agent.ports.repositories import (
    MessageRepositoryPort,
    SessionConfigRepositoryPort,
    SessionRepositoryPort,
    SummaryRepositoryPort,
    UserProfileRepositoryPort,
    UserSettingsRepositoryPort,
)
from cogito_agent.runtime.phase import BasePhase


@dataclass(frozen=True, slots=True)
class StateLoadOptions:
    recent_message_limit: int = 20
    allow_missing_session: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.recent_message_limit <= 200:
            raise ValueError(
                "recent_message_limit must be between 0 and 200"
            )


class StateLoadPhase(BasePhase):
    name = "state_load"

    def __init__(
        self,
        *,
        sessions: SessionRepositoryPort,
        messages: MessageRepositoryPort,
        summaries: SummaryRepositoryPort,
        user_profiles: UserProfileRepositoryPort,
        user_settings: UserSettingsRepositoryPort,
        session_configs: SessionConfigRepositoryPort,
        default_user_settings: UserSettings | None = None,
        default_session_config: SessionConfig | None = None,
        options: StateLoadOptions | None = None,
    ) -> None:
        self._sessions = sessions
        self._messages = messages
        self._summaries = summaries
        self._user_profiles = user_profiles
        self._user_settings = user_settings
        self._session_configs = session_configs
        self._default_user_settings = (
            default_user_settings or UserSettings()
        )
        self._default_session_config = (
            default_session_config or SessionConfig()
        )
        self._options = options or StateLoadOptions()
```

### 8.1 为什么默认值通过构造函数注入

这样可以：

- 在不同部署环境使用不同默认配置。
- 在测试中明确控制默认值。
- 避免 Phase 读取环境变量。
- 避免领域默认值散落在实现逻辑中。
- 保持 Composition Root 为配置汇总位置。

---

## 9. 第一版执行算法：顺序加载

第一版建议优先实现顺序流程，便于建立准确的错误边界和测试。

```python
from __future__ import annotations

from cogito_agent.runtime.context import TurnContext
from cogito_agent.runtime.errors import (
    MissingSessionError,
    SessionActorMismatchError,
    StateLoadError,
)


class StateLoadPhase(BasePhase):
    name = "state_load"

    # __init__ omitted

    async def execute(self, ctx: TurnContext) -> None:
        request = ctx.request

        try:
            session = await self._sessions.get(request.session_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="session",
                retryable=True,
            ) from exc

        if session is None and not self._options.allow_missing_session:
            raise MissingSessionError(request.session_id)

        if session is not None and session.actor_id != request.actor_id:
            raise SessionActorMismatchError(
                session_id=request.session_id,
                actor_id=request.actor_id,
            )

        history_limit = self._resolve_history_limit(ctx)

        try:
            recent_messages = await self._messages.list_recent(
                session_id=request.session_id,
                limit=history_limit,
            )
        except Exception as exc:
            raise StateLoadError.for_component(
                component="recent_messages",
                retryable=True,
            ) from exc

        try:
            summary = await self._summaries.get(request.session_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="session_summary",
                retryable=True,
            ) from exc

        try:
            profile = await self._user_profiles.get(request.actor_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="user_profile",
                retryable=True,
            ) from exc

        try:
            settings = await self._user_settings.get(request.actor_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="user_settings",
                retryable=True,
            ) from exc

        try:
            config = await self._session_configs.get(request.session_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="session_config",
                retryable=True,
            ) from exc

        resolved_settings = settings or self._default_user_settings
        resolved_config = config or self._default_session_config

        self._validate_loaded_state(
            ctx=ctx,
            recent_messages=recent_messages,
            config=resolved_config,
        )

        # Commit to context only after all reads and validations succeed.
        ctx.session = session
        ctx.recent_messages = list(recent_messages)
        ctx.session_summary = summary
        ctx.user_profile = profile
        ctx.user_settings = resolved_settings
        ctx.session_config = resolved_config

        if resolved_config.max_tool_rounds is not None:
            ctx.max_tool_rounds = resolved_config.max_tool_rounds
```

### 9.1 使用“先加载、后统一写入”

不要一边加载一边修改 Context：

```python
ctx.session = await sessions.get(...)
ctx.recent_messages = await messages.list_recent(...)
# 第三个读取失败，此时 ctx 已处于半完成状态
```

推荐先保存为局部变量，所有读取和验证成功后再写入 Context。这样可保证：

```text
成功：Context 获得完整确定性状态
失败：Context 不暴露半加载状态
```

虽然失败后 Kernel 会终止后续 Phase，但该规则仍有利于错误诊断、重试和单元测试。

### 9.2 历史条数计算

第一版可以只使用 `StateLoadOptions.recent_message_limit`：

```python
def _resolve_history_limit(self, ctx: TurnContext) -> int:
    return self._options.recent_message_limit
```

若会话配置本身决定历史条数，会出现“必须先加载配置才能知道消息条数”的依赖。此时调整顺序：

```text
1. Session
2. SessionConfig
3. recent_message_limit 解析
4. Messages
5. Summary
6. UserProfile
7. UserSettings
```

不要为了这个局部依赖引入 Phase DAG。

---

## 10. 第二版优化：受控并发读取

“Phase 固定顺序”不等于“Phase 内禁止并发 I/O”。当第一版稳定后，可以并行读取彼此独立的数据。

推荐使用 `asyncio.TaskGroup`：

```python
import asyncio


async def execute(self, ctx: TurnContext) -> None:
    request = ctx.request

    # 先加载 Session，因为必须先执行归属校验。
    session = await self._load_session(request.session_id)
    self._validate_session_actor(session, request.actor_id)

    async with asyncio.TaskGroup() as group:
        messages_task = group.create_task(
            self._load_recent_messages(
                session_id=request.session_id,
                limit=self._options.recent_message_limit,
            )
        )
        summary_task = group.create_task(
            self._load_summary(request.session_id)
        )
        profile_task = group.create_task(
            self._load_user_profile(request.actor_id)
        )
        settings_task = group.create_task(
            self._load_user_settings(request.actor_id)
        )
        config_task = group.create_task(
            self._load_session_config(request.session_id)
        )

    recent_messages = messages_task.result()
    summary = summary_task.result()
    profile = profile_task.result()
    settings = settings_task.result()
    config = config_task.result()

    # Validate and commit to context.
```

### 10.1 不要在第一版直接并发的原因

- `ExceptionGroup` 增加错误映射复杂度。
- 多个后端可能共用一个不支持并发的 Session。
- 测试中的调用顺序不再稳定。
- 并发收益需要通过实际延迟数据证明。

### 10.2 并发前提

只有满足以下条件才并发：

- Repository Adapter 使用独立连接或并发安全连接池。
- 各读取之间没有事务快照一致性要求。
- 已定义多个任务同时失败时的错误优先级。
- 已有超时和取消测试。

---

## 11. 错误模型

建议在 `runtime/errors.py` 中增加：

```python
from __future__ import annotations


class StateLoadError(RuntimeAgentError):
    code = "STATE_LOAD_ERROR"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        component: str,
        safe_message: str = "加载会话状态失败",
        retryable: bool = True,
    ) -> None:
        super().__init__(message, safe_message=safe_message)
        self.component = component
        self.retryable = retryable

    @classmethod
    def for_component(
        cls,
        *,
        component: str,
        retryable: bool,
    ) -> "StateLoadError":
        return cls(
            f"Failed to load deterministic state component: {component}",
            component=component,
            retryable=retryable,
        )


class MissingSessionError(RuntimeAgentError):
    code = "SESSION_NOT_FOUND"
    retryable = False

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Session not found: {session_id}",
            safe_message="会话不存在",
        )


class SessionActorMismatchError(RuntimeAgentError):
    code = "SESSION_ACCESS_DENIED"
    retryable = False

    def __init__(self, *, session_id: str, actor_id: str) -> None:
        super().__init__(
            f"Actor {actor_id} cannot access session {session_id}",
            safe_message="当前会话不可访问",
        )


class InvalidLoadedStateError(RuntimeAgentError):
    code = "INVALID_LOADED_STATE"
    retryable = False
```

### 11.1 错误分类

| 场景 | 错误码 | retryable |
|---|---|---:|
| 数据库暂时不可用 | `STATE_LOAD_ERROR` | 是 |
| Session 不存在且策略要求存在 | `SESSION_NOT_FOUND` | 否 |
| Session 不属于当前 actor | `SESSION_ACCESS_DENIED` | 否 |
| 历史记录跨 Session | `INVALID_LOADED_STATE` | 否 |
| 配置值越界 | `INVALID_LOADED_STATE` | 否 |

### 11.2 不要捕获取消异常

在 Python 3.12 中，Phase 的通用异常包装必须确保取消可以继续向上传播。建议只包装预期的 Repository 异常类型；若暂时只能捕获 `Exception`，也不要捕获 `BaseException`。

Kernel 负责单独处理 `asyncio.CancelledError`。

---

## 12. 状态验证

建议至少检查：

```python
def _validate_loaded_state(
    self,
    *,
    ctx: TurnContext,
    recent_messages: list[ConversationMessage],
    config: SessionConfig,
) -> None:
    request = ctx.request

    for message in recent_messages:
        if message.session_id != request.session_id:
            raise InvalidLoadedStateError(
                "Repository returned message from another session"
            )

    sequences = [message.sequence for message in recent_messages]
    if sequences != sorted(sequences):
        raise InvalidLoadedStateError(
            "Recent messages must be ordered by sequence ascending"
        )

    if config.max_tool_rounds is not None:
        if not 1 <= config.max_tool_rounds <= 32:
            raise InvalidLoadedStateError(
                "max_tool_rounds must be between 1 and 32"
            )
```

是否在 Phase 做强校验取决于 Port 的可信程度。推荐：

- Adapter 负责把数据库行转换为合法领域对象。
- Phase 负责验证跨对象关系和本轮请求约束。

---

## 13. 超时策略

第一版可以依赖 Kernel 或应用层统一超时。若要在 Phase 内设置读取超时，建议注入配置，而不是写死：

```python
@dataclass(frozen=True, slots=True)
class StateLoadOptions:
    recent_message_limit: int = 20
    allow_missing_session: bool = True
    timeout_seconds: float | None = 5.0
```

```python
import asyncio


async def execute(self, ctx: TurnContext) -> None:
    if self._options.timeout_seconds is None:
        await self._execute_without_timeout(ctx)
        return

    try:
        async with asyncio.timeout(self._options.timeout_seconds):
            await self._execute_without_timeout(ctx)
    except TimeoutError as exc:
        raise StateLoadError(
            "State loading timed out",
            component="all",
            safe_message="加载会话状态超时",
            retryable=True,
        ) from exc
```

不要使用会吞掉任务取消的自定义超时封装。

---

## 14. 事件与可观测性

Kernel 已统一发出：

```text
PHASE_STARTED(state_load)
PHASE_COMPLETED(state_load)
PHASE_FAILED(state_load)
```

`StateLoadPhase` 不应直接发布 MessageBus 事件。

第一版建议只增加结构化日志：

```python
logger.debug(
    "Deterministic state loaded",
    extra={
        "turn_id": ctx.turn_id,
        "request_id": ctx.request.request_id,
        "session_found": session is not None,
        "recent_message_count": len(recent_messages),
        "summary_found": summary is not None,
        "profile_found": profile is not None,
        "settings_source": "stored" if settings else "default",
        "config_source": "stored" if config else "default",
    },
)
```

禁止记录：

- 消息正文。
- Summary 全文。
- 用户隐私字段。
- 数据库连接对象。
- 原始认证信息。

若后续需要每个 Repository 的耗时，可在 Adapter 或 tracing decorator 中统一实现，不要在 Phase 中复制计时代码。

---

## 15. Composition Root 组装

```python
from cogito_agent.domain.state import SessionConfig, UserSettings
from cogito_agent.runtime.phases.state_load import (
    StateLoadOptions,
    StateLoadPhase,
)


state_load = StateLoadPhase(
    sessions=session_repository,
    messages=message_repository,
    summaries=summary_repository,
    user_profiles=user_profile_repository,
    user_settings=user_settings_repository,
    session_configs=session_config_repository,
    default_user_settings=UserSettings(
        locale="zh-CN",
        timezone="Asia/Tokyo",
        tool_approval_mode="default",
    ),
    default_session_config=SessionConfig(
        history_limit=20,
        max_tool_rounds=8,
    ),
    options=StateLoadOptions(
        recent_message_limit=20,
        allow_missing_session=True,
    ),
)

phases: list[RuntimePhase] = [
    TurnInitPhase(clock=clock, id_generator=id_generator),
    state_load,
    InformationRetrievalPhase(...),
    ContextAssemblyPhase(...),
    AgentLoopPhase(...),
    KnowledgeExtractionPhase(...),
    PersistencePhase(...),
    TurnFinalizePhase(),
]
```

Phase 不允许在构造函数中创建数据库连接，也不允许从全局 Service Locator 获取 Repository。

---

## 16. 测试实现路径

推荐新增：

```text
cogito_agent/tests/unit/runtime/phases/test_state_load.py
```

### 16.1 Fake Repository

```python
from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class FakeGetRepository(Generic[T]):
    def __init__(
        self,
        value: T | None = None,
        error: Exception | None = None,
    ) -> None:
        self.value = value
        self.error = error
        self.calls: list[str] = []

    async def get(self, key: str) -> T | None:
        self.calls.append(key)
        if self.error is not None:
            raise self.error
        return self.value


class FakeMessageRepository:
    def __init__(
        self,
        messages: list[ConversationMessage] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.messages = messages or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def list_recent(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        self.calls.append((session_id, limit))
        if self.error is not None:
            raise self.error
        return list(self.messages)
```

### 16.2 必测用例

#### 用例 A：完整状态加载

验证：

- 每个 Repository 使用正确 ID 调用一次。
- Context 所有字段被正确填充。
- 不修改检索、模型和输出字段。

#### 用例 B：新会话缺少可选状态

Repository 返回：

```text
session=None
messages=[]
summary=None
profile=None
settings=None
config=None
```

验证：

- Phase 成功。
- 使用默认 `UserSettings`。
- 使用默认 `SessionConfig`。
- Context 不包含伪造 Session。

#### 用例 C：禁止缺失 Session

设置：

```python
StateLoadOptions(allow_missing_session=False)
```

验证抛出 `MissingSessionError`。

#### 用例 D：Session actor 不匹配

验证：

- 抛出 `SessionActorMismatchError`。
- 后续 Repository 不应继续调用。
- 错误消息不泄漏实际 Session 所属 actor。

#### 用例 E：Repository 故障

对每个 Repository 分别注入异常，验证：

- 映射为 `StateLoadError`。
- `component` 正确。
- `retryable=True`。
- `__cause__` 保留原始异常。

#### 用例 F：Context 原子更新

让最后一个 Repository 抛错，验证此前字段仍保持进入 Phase 前的值。

#### 用例 G：消息归属异常

Repository 返回其他 `session_id` 的消息，验证抛出 `InvalidLoadedStateError`。

#### 用例 H：消息顺序异常

返回 sequence：

```text
[3, 1, 2]
```

验证抛出 `InvalidLoadedStateError`。

#### 用例 I：配置覆盖运行限制

SessionConfig 指定：

```python
max_tool_rounds=4
```

验证：

```python
ctx.max_tool_rounds == 4
```

#### 用例 J：Phase 不执行写操作

使用只提供读取方法的 Fake Port 即可完成测试；不得要求 `commit()`、`save()` 或 UnitOfWork。

### 16.3 示例测试

```python
import pytest


@pytest.mark.asyncio
async def test_loads_deterministic_state_into_context() -> None:
    session = SessionState(
        session_id="s-1",
        actor_id="u-1",
    )
    settings = UserSettings(
        locale="zh-CN",
        timezone="Asia/Tokyo",
    )
    config = SessionConfig(
        history_limit=20,
        max_tool_rounds=4,
    )

    phase = StateLoadPhase(
        sessions=FakeGetRepository(session),
        messages=FakeMessageRepository([]),
        summaries=FakeGetRepository(None),
        user_profiles=FakeGetRepository(None),
        user_settings=FakeGetRepository(settings),
        session_configs=FakeGetRepository(config),
    )

    ctx = TurnContext(
        request=AgentRequest(
            request_id="r-1",
            session_id="s-1",
            actor_id="u-1",
            text="hello",
        )
    )

    await phase.run(ctx)

    assert ctx.session == session
    assert ctx.recent_messages == []
    assert ctx.user_settings == settings
    assert ctx.session_config == config
    assert ctx.max_tool_rounds == 4
    assert ctx.retrieved_items == []
    assert ctx.model_messages == []
    assert ctx.output_text is None
```

---

## 17. 架构边界测试

应增加静态测试，防止 `state_load.py` 导入基础设施实现：

```python
FORBIDDEN_IMPORT_PREFIXES = (
    "sqlalchemy",
    "redis",
    "asyncpg",
    "pymongo",
    "nats",
    "kafka",
    "telegram",
    "discord",
    "fastapi",
    "starlette",
    "cogito_agent.infrastructure",
    "cogito_agent.application.messaging",
)
```

`StateLoadPhase` 允许导入：

```text
cogito_agent.domain.*
cogito_agent.ports.*
cogito_agent.runtime.context
cogito_agent.runtime.errors
cogito_agent.runtime.phase
Python 标准库
```

---

## 18. 不应采用的实现

### 18.1 在 Phase 中写 SQL

```python
# 错误
await db.execute("SELECT * FROM sessions WHERE id = ?", ...)
```

原因：破坏 Port 边界，无法独立测试。

### 18.2 把所有状态放入 metadata

```python
# 错误
ctx.metadata["session"] = session
ctx.metadata["history"] = history
```

原因：绕过强类型 `TurnContext`。

### 18.3 把存储故障当作空数据

```python
# 错误
try:
    return await repository.get(...)
except Exception:
    return None
```

原因：会把系统故障伪装成正常的新会话。

### 18.4 在读取阶段创建 Session

```python
# 错误
if session is None:
    await sessions.create(...)
```

原因：引入写操作与事务边界，应放到 `PersistencePhase` 或独立 Session Provisioning 机制。

### 18.5 在本阶段执行相关性检索

```python
# 错误
await vector_store.search(ctx.request.text)
```

原因：属于 `InformationRetrievalPhase`。

### 18.6 直接构建模型消息

```python
# 错误
ctx.model_messages.append(...)
```

原因：属于 `ContextAssemblyPhase`。

### 18.7 在 Phase 内发布 MessageBus 事件

```python
# 错误
await bus.publish("agent.state.loaded", ...)
```

原因：Kernel 与 Phase 必须保持 MessageBus 无关。

### 18.8 引入通用 DAG

```python
# 错误
requires = {"turn_init"}
produces = {"session", "history"}
```

原因：Phase 顺序由 Composition Root 显式列表决定。

---

## 19. 分阶段交付计划

### Step 1：领域类型

交付：

- `SessionState`
- `ConversationMessage`
- `SessionSummary`
- `UserProfile`
- `UserSettings`
- `SessionConfig`

验收：mypy/pyright 能识别 `TurnContext` 确定性状态字段。

### Step 2：读取 Port

交付：

- 补全现有 Session、Message、Summary Repository 类型。
- 新增 UserProfile、UserSettings、SessionConfig Repository Port。

验收：Port 不导入任何数据库具体类型。

### Step 3：Context 强类型化

交付：

- 替换 `object` 占位。
- 增加 `session_config` 正式字段。

验收：下游无需访问 `ctx.metadata` 读取核心状态。

### Step 4：StateLoadPhase 顺序实现

交付：

- 构造函数依赖注入。
- 顺序只读加载。
- Session 归属验证。
- 默认设置与配置合并。
- 原子写入 Context。

验收：完整状态、新会话和失败路径单元测试通过。

### Step 5：错误映射

交付：

- `StateLoadError`
- `MissingSessionError`
- `SessionActorMismatchError`
- `InvalidLoadedStateError`

验收：Kernel 可以将错误映射成稳定安全消息。

### Step 6：Composition Root

交付：

- 注入真实或 Fake Adapter。
- 注入默认设置、默认配置和加载选项。

验收：Kernel 和 Phase 不自行创建 Adapter。

### Step 7：可观测性

交付：

- 结构化日志。
- 不记录内容正文。
- 依赖 Kernel 的 Phase 生命周期事件。

### Step 8：性能优化（可选）

在基准数据证明必要后：

- 使用 `TaskGroup` 并发独立读取。
- 增加超时。
- 增加 Repository 层缓存装饰器。

这些优化不得改变 Phase 的输入输出契约。

---

## 20. 完成定义（Definition of Done）

- [ ] `StateLoadPhase` 只依赖 Domain Model、Runtime Context 和 Port。
- [ ] 只根据 `session_id`、`actor_id` 等明确标识加载状态。
- [ ] 不执行关键词检索或向量检索。
- [ ] 不构建 Prompt 或 `model_messages`。
- [ ] 不调用模型或工具。
- [ ] 不写数据库，不开启提交事务。
- [ ] 不发布 MessageBus 消息。
- [ ] Session 不存在与存储故障可明确区分。
- [ ] Session 与 actor 的归属关系得到验证。
- [ ] 最近消息的数量、归属与顺序得到约束。
- [ ] 用户设置与会话配置具有强类型默认值。
- [ ] Context 在全部读取成功后才统一更新。
- [ ] Repository 异常映射为稳定 Runtime Error。
- [ ] 取消异常可以正常传播到 Kernel。
- [ ] 单元测试覆盖成功、缺失、拒绝、异常和原子更新路径。
- [ ] 架构测试确认没有 Infrastructure、Channel 或 MessageBus 依赖。

---

## 21. 最终推荐实现形态

```text
RuntimeKernel
    │
    ▼
StateLoadPhase
    │
    ├── SessionRepositoryPort
    ├── MessageRepositoryPort
    ├── SummaryRepositoryPort
    ├── UserProfileRepositoryPort
    ├── UserSettingsRepositoryPort
    └── SessionConfigRepositoryPort
          │
          ▼
Infrastructure Adapters
(SQL / document DB / API / cache decorator)
```

核心原则可以压缩为一句话：

> `StateLoadPhase` 负责把“明确 ID 对应的只读状态快照”装载进强类型 `TurnContext`；凡是需要相关性计算、模型推理、写操作或消息发布的逻辑，都不属于这个 Phase。
