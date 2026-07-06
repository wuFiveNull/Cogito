# TurnInitPhase 具体实现路径

> 基于《Cogito-Agent 初始框架实现规格》整理。本文聚焦 `TurnInitPhase` 的职责边界、依赖、状态变更、异常语义、代码结构、装配方式和测试策略。

## 1. 实现目标

`TurnInitPhase` 是 Agent Turn 的初始化边界。它只负责让一个刚创建的 `TurnContext` 进入“可被后续阶段安全使用”的状态，不加载业务数据，不执行检索，不构建 Prompt，也不调用模型或发布 MessageBus 消息。

完成后应满足：

- 请求基础字段已经校验。
- 本轮拥有稳定的 `turn_id`。
- `started_at` 已记录。
- Trace 已启动并写入 `trace_id`。
- 运行参数已初始化，例如 `max_tool_rounds` 和超时配置。
- Context 中不应存在来自上一轮的残留状态。
- 任何失败都以稳定的 Runtime Error 暴露。
- 不产生伪造的 Session、检索结果、模型结果或持久化结果。

---

## 2. 职责边界

### 2.1 本阶段负责

1. 校验 `AgentRequest` 的基础完整性。
2. 生成或确认 `turn_id`。
3. 记录本轮开始时间。
4. 初始化 Trace。
5. 初始化运行限制。
6. 校验 Context 的初始状态。
7. 写入少量与本轮运行有关的元数据。

### 2.2 本阶段禁止

- 调用 Session、Message、Preference、Memory Repository。
- 执行关键词检索、向量检索或 Rerank。
- 构建 `model_messages`。
- 加载工具清单。
- 调用模型或工具。
- 写数据库。
- 生成 `TurnResult`。
- 发送 MessageBus Envelope。
- 读取 Telegram、Discord、HTTP、WebSocket 等 Channel 类型。

---

## 3. 一个必须先解决的事件顺序问题

规格中的推荐 Kernel 流程在执行第一个 Phase 之前发送：

```text
TURN_STARTED
PHASE_STARTED(turn_init)
```

但 `AgentEvent.turn_id` 被定义为非空字符串，而 `turn_id` 又计划由 `TurnInitPhase` 生成。这样会出现循环依赖：

```text
发送 TURN_STARTED 需要 turn_id
生成 turn_id 需要先执行 TurnInitPhase
执行 TurnInitPhase 前又要发送 PHASE_STARTED
```

### 3.1 推荐修正：预初始化身份，TurnInit 完成业务初始化

推荐引入一个极薄的 `TurnContextFactory`，在任何事件发出前创建本轮身份：

```text
AgentRequest
    ↓
TurnContextFactory
    ├── turn_id
    ├── started_at
    └── status = RUNNING
    ↓
RuntimeKernel 发出 TURN_STARTED
    ↓
TurnInitPhase
    ├── 请求校验
    ├── Trace 初始化
    ├── Runtime Limits
    └── 初始状态校验
```

这样可以保持：

- `AgentEvent.turn_id` 非空。
- Kernel 不需要对 `turn_init` 写名称分支。
- Phase Pipeline 仍然按普通列表统一执行。
- `TurnInitPhase` 仍是第一个业务初始化阶段。
- 不需要将事件发送逻辑下放到 Phase。

### 3.2 不推荐方案

以下方案不建议采用：

- 将 `AgentEvent.turn_id` 改成可空。
- 使用 `request_id` 临时代替 `turn_id`。
- 在 Kernel 中写 `if phase.name == "turn_init"`。
- 让 `TurnInitPhase` 自己发送 Kernel 生命周期事件。
- 在 `TurnInitPhase` 完成前不记录任何 Phase 事件。

---

## 4. 推荐目录位置

```text
cogito_agent/
├── runtime/
│   ├── context.py
│   ├── context_factory.py
│   ├── errors.py
│   └── phases/
│       └── turn_init.py
├── ports/
│   ├── clock.py
│   ├── ids.py
│   └── tracing.py
└── tests/
    └── unit/
        └── runtime/
            └── phases/
                └── test_turn_init.py
```

---

## 5. 输入与输出

### 5.1 输入

`TurnInitPhase` 接受：

```python
TurnContext(request=AgentRequest(...))
```

进入 Phase 前，推荐由 `TurnContextFactory` 保证：

```python
ctx.turn_id is not None
ctx.started_at is not None
ctx.status is TurnStatus.RUNNING
ctx.trace_id is None
ctx.current_phase == "turn_init"
```

### 5.2 输出

成功完成后：

```python
ctx.turn_id is not None
ctx.started_at is not None
ctx.trace_id is not None
ctx.status is TurnStatus.RUNNING
ctx.max_tool_rounds > 0
ctx.error is None
ctx.result is None
ctx.output_text is None
ctx.persistence_completed is False
```

该 Phase 不返回值，所有结果写入 `TurnContext`：

```python
async def run(self, ctx: TurnContext) -> None:
    ...
```

---

## 6. 请求校验规则

第一版应使用明确、稳定、低争议的校验规则。

### 6.1 必填标识

以下字段去除首尾空白后必须非空：

- `request_id`
- `session_id`
- `actor_id`

### 6.2 输入内容

推荐允许以下两种输入：

```text
text 非空
或
attachments 非空
```

因此，仅当文本为空且附件也为空时拒绝请求：

```python
if not request.text.strip() and not request.attachments:
    raise InvalidAgentRequestError(
        "Agent request contains neither text nor attachments",
        safe_message="请求内容不能为空",
    )
```

这样可以支持纯图片、纯文件等未来 Channel 场景，同时保持 Kernel 与 Channel 解耦。

### 6.3 附件校验

每个 `AttachmentRef` 至少校验：

- `attachment_id` 非空。
- `media_type` 非空。
- 同一请求中 `attachment_id` 不重复。

本阶段不下载附件、不检查 URI 可访问性，也不解析媒体内容。

### 6.4 Metadata

本阶段不应深度解释 Channel metadata。只做以下安全检查：

- 必须是 Mapping。
- 不允许将 Exception、数据库连接、SDK Message 对象等写入运行事件。
- 不将 metadata 原样复制到 AgentEvent。

---

## 7. 运行配置

建议把初始化参数定义为不可变配置对象：

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnInitConfig:
    max_tool_rounds: int = 8
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_tool_rounds <= 0:
            raise ValueError("max_tool_rounds must be greater than zero")

        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
```

Context 已有正式字段 `max_tool_rounds`，因此直接写入：

```python
ctx.max_tool_rounds = self._config.max_tool_rounds
```

若当前 `TurnContext` 尚无 `timeout_seconds` 正式字段，第一版可以临时写入：

```python
ctx.metadata["timeout_seconds"] = self._config.timeout_seconds
```

但后续一旦超时成为稳定运行语义，应将其提升为 `TurnContext` 的正式字段，而不是长期留在 `metadata`。

---

## 8. Context Factory 实现

```python
from __future__ import annotations

from dataclasses import dataclass

from cogito_agent.ports.clock import ClockPort
from cogito_agent.ports.ids import IdGeneratorPort
from cogito_agent.runtime.context import TurnContext
from cogito_agent.runtime.models import AgentRequest, TurnStatus


@dataclass(slots=True)
class TurnContextFactory:
    clock: ClockPort
    id_generator: IdGeneratorPort

    def create(self, request: AgentRequest) -> TurnContext:
        return TurnContext(
            request=request,
            turn_id=self.id_generator.new_id(),
            started_at=self.clock.now(),
            status=TurnStatus.RUNNING,
        )
```

### 8.1 Factory 的边界

Factory 只负责构造事件系统需要的最小身份信息：

- 不启动 Trace。
- 不校验业务请求。
- 不加载任何 Repository。
- 不配置工具轮数。
- 不发送事件。
- 不包含 Channel 或 MessageBus 类型。

---

## 9. TurnInitPhase 代码骨架

```python
from __future__ import annotations

from dataclasses import dataclass

from cogito_agent.ports.tracing import RuntimeTracePort
from cogito_agent.runtime.context import TurnContext
from cogito_agent.runtime.errors import (
    InvalidAgentRequestError,
    PhaseExecutionError,
)
from cogito_agent.runtime.models import AgentRequest, TurnStatus
from cogito_agent.runtime.phase import BasePhase


@dataclass(frozen=True, slots=True)
class TurnInitConfig:
    max_tool_rounds: int = 8
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_tool_rounds <= 0:
            raise ValueError("max_tool_rounds must be greater than zero")

        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")


class TurnInitPhase(BasePhase):
    name = "turn_init"

    def __init__(
        self,
        *,
        trace: RuntimeTracePort,
        config: TurnInitConfig | None = None,
    ) -> None:
        self._trace = trace
        self._config = config or TurnInitConfig()

    async def execute(self, ctx: TurnContext) -> None:
        self._validate_context_identity(ctx)
        self._validate_request(ctx.request)
        self._validate_clean_context(ctx)

        ctx.max_tool_rounds = self._config.max_tool_rounds

        if self._config.timeout_seconds is not None:
            ctx.metadata["timeout_seconds"] = self._config.timeout_seconds

        try:
            ctx.trace_id = await self._trace.start_turn(
                turn_id=ctx.turn_id,
                request_id=ctx.request.request_id,
            )
        except Exception as exc:
            raise PhaseExecutionError(
                phase=self.name,
                message="Failed to initialize runtime trace",
                safe_message="初始化运行环境失败",
            ) from exc

    @staticmethod
    def _validate_context_identity(ctx: TurnContext) -> None:
        if not ctx.turn_id:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message="TurnContext.turn_id must be initialized before TurnInitPhase",
                safe_message="初始化运行环境失败",
            )

        if ctx.started_at is None:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message="TurnContext.started_at must be initialized before TurnInitPhase",
                safe_message="初始化运行环境失败",
            )

        if ctx.status is not TurnStatus.RUNNING:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message=f"Unexpected initial turn status: {ctx.status}",
                safe_message="初始化运行状态无效",
            )

    @staticmethod
    def _validate_request(request: AgentRequest) -> None:
        required_fields = {
            "request_id": request.request_id,
            "session_id": request.session_id,
            "actor_id": request.actor_id,
        }

        for field_name, value in required_fields.items():
            if not value.strip():
                raise InvalidAgentRequestError(
                    f"{field_name} must not be blank",
                    safe_message="请求标识不完整",
                )

        if not request.text.strip() and not request.attachments:
            raise InvalidAgentRequestError(
                "Request must contain text or at least one attachment",
                safe_message="请求内容不能为空",
            )

        attachment_ids: set[str] = set()

        for attachment in request.attachments:
            if not attachment.attachment_id.strip():
                raise InvalidAgentRequestError(
                    "attachment_id must not be blank",
                    safe_message="附件标识无效",
                )

            if not attachment.media_type.strip():
                raise InvalidAgentRequestError(
                    "attachment media_type must not be blank",
                    safe_message="附件类型无效",
                )

            if attachment.attachment_id in attachment_ids:
                raise InvalidAgentRequestError(
                    f"Duplicate attachment_id: {attachment.attachment_id}",
                    safe_message="请求中存在重复附件",
                )

            attachment_ids.add(attachment.attachment_id)

    @staticmethod
    def _validate_clean_context(ctx: TurnContext) -> None:
        if ctx.trace_id is not None:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message="Trace has already been initialized",
                safe_message="本轮运行状态重复初始化",
            )

        dirty_fields = {
            "retrieved_items": bool(ctx.retrieved_items),
            "model_messages": bool(ctx.model_messages),
            "model_responses": bool(ctx.model_responses),
            "tool_records": bool(ctx.tool_records),
            "preference_candidates": bool(ctx.preference_candidates),
            "memory_candidates": bool(ctx.memory_candidates),
            "output_text": ctx.output_text is not None,
            "result": ctx.result is not None,
            "persistence_completed": ctx.persistence_completed,
        }

        dirty = [name for name, populated in dirty_fields.items() if populated]

        if dirty:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message=f"TurnContext is not clean: {', '.join(dirty)}",
                safe_message="本轮运行状态无效",
            )
```

---

## 10. Error 类型建议

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


class InvalidAgentRequestError(RuntimeAgentError):
    code = "INVALID_AGENT_REQUEST"
    retryable = False


class PhaseExecutionError(RuntimeAgentError):
    code = "PHASE_EXECUTION_ERROR"
    retryable = False

    def __init__(
        self,
        *,
        phase: str,
        message: str,
        safe_message: str | None = None,
    ) -> None:
        super().__init__(message, safe_message=safe_message)
        self.phase = phase
```

### 10.1 错误分类

| 场景 | 错误类型 | retryable |
|---|---|---:|
| request_id 为空 | `InvalidAgentRequestError` | false |
| 无文本且无附件 | `InvalidAgentRequestError` | false |
| Context 已污染 | `PhaseExecutionError` | false |
| turn_id 未预初始化 | `PhaseExecutionError` | false |
| Trace Adapter 临时不可用 | 可映射为 Trace/Phase 错误 | 视 Adapter 策略 |
| Task 被取消 | `asyncio.CancelledError` 原样传播 | 不包装 |

`CancelledError` 不应被 `except Exception` 误处理。Python 版本和继承关系应通过测试确认；最安全的做法是在 Kernel 中单独捕获取消，并保持原样传播。

---

## 11. Kernel 集成方式

Kernel 构造时注入 `TurnContextFactory`：

```python
class RuntimeKernel:
    def __init__(
        self,
        phases: Sequence[RuntimePhase],
        *,
        context_factory: TurnContextFactory,
        default_event_sink: AgentEventSink | None = None,
        cleanup: RuntimeCleanup | None = None,
        error_mapper: RuntimeErrorMapper | None = None,
    ) -> None:
        self._phases = tuple(phases)
        self._context_factory = context_factory
        ...
```

运行时：

```python
async def run(
    self,
    request: AgentRequest,
    *,
    event_sink: AgentEventSink | None = None,
) -> TurnResult:
    sink = event_sink or self._default_event_sink
    ctx = self._context_factory.create(request)

    try:
        await emit_safely(self._events.turn_started(ctx))

        for phase in self._phases:
            ctx.current_phase = phase.name
            await emit_safely(self._events.phase_started(ctx, phase.name))

            try:
                await phase.run(ctx)
            except Exception as exc:
                await emit_safely(
                    self._events.phase_failed(ctx, phase.name, exc)
                )
                raise

            await emit_safely(
                self._events.phase_completed(ctx, phase.name)
            )

        ...
    finally:
        await self._cleanup.run(ctx)
```

Kernel 不需要知道哪个 Phase 是 `turn_init`，也不需要添加名称分支。

---

## 12. Composition Root

```python
def build_runtime_kernel(
    *,
    clock: ClockPort,
    id_generator: IdGeneratorPort,
    trace: RuntimeTracePort,
    event_sink: AgentEventSink | None = None,
) -> RuntimeKernel:
    context_factory = TurnContextFactory(
        clock=clock,
        id_generator=id_generator,
    )

    phases: list[RuntimePhase] = [
        TurnInitPhase(
            trace=trace,
            config=TurnInitConfig(
                max_tool_rounds=8,
                timeout_seconds=None,
            ),
        ),
        StateLoadPhase(),
        InformationRetrievalPhase(),
        ContextAssemblyPhase(),
        AgentLoopPhase(),
        KnowledgeExtractionPhase(),
        PersistencePhase(),
        TurnFinalizePhase(),
    ]

    return RuntimeKernel(
        phases=phases,
        context_factory=context_factory,
        default_event_sink=event_sink or NullAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
```

---

## 13. Trace 生命周期

推荐所有权如下：

```text
TurnContextFactory
    └── 生成 turn_id / started_at

TurnInitPhase
    └── trace.start_turn()

RuntimeCleanup
    └── trace.end_turn()
```

Cleanup 应根据最终状态结束 Trace：

```python
await trace.end_turn(
    trace_id=ctx.trace_id,
    status=ctx.status.value,
)
```

注意：

- `trace_id` 可能为空，例如 Trace 初始化前已经失败。
- Cleanup 必须处理空 `trace_id`。
- Cleanup 失败不得覆盖原始异常。
- TurnInitPhase 不负责结束 Trace。

---

## 14. 单元测试清单

### 14.1 正常文本请求

验证：

- Trace 被调用一次。
- `turn_id`、`started_at` 保持不变。
- `trace_id` 被写入。
- `max_tool_rounds` 被设置。
- 不产生任何检索、模型或持久化数据。

### 14.2 纯附件请求

```python
request.text == ""
request.attachments != ()
```

应允许通过。

### 14.3 空请求

```python
request.text.strip() == ""
request.attachments == ()
```

应抛 `InvalidAgentRequestError`。

### 14.4 空标识

分别测试：

- 空 `request_id`
- 空 `session_id`
- 空 `actor_id`
- 只有空白字符

### 14.5 附件错误

分别测试：

- 空 `attachment_id`
- 空 `media_type`
- 重复 `attachment_id`

### 14.6 Context 未预初始化

分别测试：

- `turn_id is None`
- `started_at is None`
- `status != RUNNING`

应抛 `PhaseExecutionError`，且 Trace 不应被调用。

### 14.7 Context 已污染

预先写入任一字段：

- `retrieved_items`
- `model_messages`
- `tool_records`
- `output_text`
- `result`
- `persistence_completed`

应拒绝初始化，避免 Context 被跨请求复用。

### 14.8 Trace 启动失败

Fake Trace 抛异常时：

- Phase 抛稳定 Runtime Error。
- `trace_id` 保持为空。
- Kernel 发送 `PHASE_FAILED` 和 `TURN_FAILED`。
- Kernel Cleanup 仍执行。
- 后续 Phase 不执行。

### 14.9 Event 顺序

推荐验证：

```text
TURN_STARTED
PHASE_STARTED(turn_init)
PHASE_COMPLETED(turn_init)
PHASE_STARTED(state_load)
...
```

此时 `TURN_STARTED.turn_id` 已由 Factory 保证非空。

---

## 15. 示例测试代码

```python
import pytest

from cogito_agent.runtime.context import TurnContext
from cogito_agent.runtime.models import AgentRequest, TurnStatus
from cogito_agent.runtime.phases.turn_init import (
    TurnInitConfig,
    TurnInitPhase,
)


class FakeTrace:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def start_turn(
        self,
        *,
        turn_id: str,
        request_id: str,
    ) -> str:
        self.calls.append((turn_id, request_id))
        return "trace-001"


@pytest.mark.asyncio
async def test_turn_init_initializes_trace_and_limits() -> None:
    trace = FakeTrace()
    phase = TurnInitPhase(
        trace=trace,
        config=TurnInitConfig(max_tool_rounds=5),
    )

    request = AgentRequest(
        request_id="request-001",
        session_id="session-001",
        actor_id="actor-001",
        text="hello",
    )

    ctx = TurnContext(
        request=request,
        turn_id="turn-001",
        started_at=FIXED_TIME,
        status=TurnStatus.RUNNING,
    )

    await phase.run(ctx)

    assert ctx.trace_id == "trace-001"
    assert ctx.max_tool_rounds == 5
    assert trace.calls == [("turn-001", "request-001")]
    assert ctx.retrieved_items == []
    assert ctx.model_messages == []
    assert ctx.result is None
```

---

## 16. 验收标准

- [ ] `TurnInitPhase` 不导入 Repository、Model、Tool、MessageBus 或 Channel Adapter。
- [ ] 所有依赖通过构造函数注入。
- [ ] 请求标识为空时产生稳定错误。
- [ ] 支持“文本或附件至少一个存在”。
- [ ] 不下载或解析附件。
- [ ] 不制造 Session、历史、检索结果或模型结果。
- [ ] Trace 启动失败不会被静默吞掉。
- [ ] `turn_id` 在第一个 AgentEvent 发出前已存在。
- [ ] Kernel 中没有 `if phase.name == "turn_init"`。
- [ ] Context 污染能够被检测。
- [ ] `max_tool_rounds` 使用正式字段。
- [ ] 临时超时配置仅在尚无正式字段时放入 metadata。
- [ ] 成功、失败、取消路径最终都由 Cleanup 收尾。
- [ ] 单元测试覆盖正常、非法请求、污染状态和 Trace 失败。

---

## 17. 最终执行路径

```text
1. Channel Adapter 将输入转换为 MessageEnvelope
2. AgentMessageWorker 将 Envelope 映射为 AgentRequest
3. AgentApplicationService 调用 RuntimeKernel
4. TurnContextFactory 创建 Context
   - turn_id
   - started_at
   - status = RUNNING
5. Kernel 发送 TURN_STARTED
6. Kernel 发送 PHASE_STARTED(turn_init)
7. TurnInitPhase
   - 校验 Context identity
   - 校验 AgentRequest
   - 校验 Context 无残留
   - 设置 max_tool_rounds / timeout
   - 启动 Trace
8. Kernel 发送 PHASE_COMPLETED(turn_init)
9. 进入 StateLoadPhase
10. 任意失败由 Kernel 映射错误并在 finally 中执行 Cleanup
```

这个实现将 `TurnInitPhase` 保持为职责单一、可测试、无 Channel/MessageBus 依赖的初始化阶段，同时解决了 `turn_id` 与生命周期事件之间的时序冲突。
