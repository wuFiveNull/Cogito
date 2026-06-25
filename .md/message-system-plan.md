# Cogito v1 — 消息系统完整架构（修订版）

## 1. 设计目标

消息系统是 Agent 的神经系统：所有外部输入从这里进入，所有 Agent 输出从这里发出。

本系统面向个人使用场景，不追求超高并发或分布式部署，但必须保证边界清晰、行为可追踪、故障可恢复，并为后续能力扩展保留稳定接口。

核心目标：

1. **Channel 可扩展**  
   新增 Telegram、QQ、Slack、IPC 等信道时，不修改 Agent 核心处理逻辑。

2. **消息生命周期可观测**  
   每条消息从接收、排队、处理、提交到投递，都具有稳定标识和状态记录。

3. **入站与出站解耦**  
   外部消息接收、Agent 推理和外部消息投递互不阻塞。

4. **同会话有序，跨会话有限并行**  
   同一 Session 内的消息严格按顺序处理，不同 Session 可在受控范围内并行。

5. **所有出站统一治理**  
   被动回复、主动推送和工具调用统一经过 DeliveryManager，复用持久化、重试、权限和可观测机制。

6. **故障可恢复**  
   Agent 重启后，可恢复未完成的出站投递，不因内存队列丢失关键消息。

7. **未来可接入**  
   Phase 管道、记忆系统、Proactive、插件、MCP 工具等均通过稳定扩展点接入。

---

## 2. 核心设计原则

### 2.1 Channel 只负责协议适配

Channel 只做两件事：

- 将外部平台事件转换成标准入站消息；
- 将标准出站请求转换成平台 API 调用。

Channel 不直接操作：

- Session；
- AgentLoop；
- EventBus；
- ToolRegistry；
- 中断控制器；
- 主动推送工具。

### 2.2 所有出站统一进入 DeliveryManager

以下三类出站都提交为 `OutboundRequest`：

- 用户消息触发的被动回复；
- Proactive 后台任务触发的主动推送；
- LLM 调用 `message_push` 工具触发的消息。

三者只在 `origin`、权限和优先级上不同，投递机制完全一致。

### 2.3 核心事务不依赖 EventBus

Session 持久化、Turn 提交和 Outbox 写入属于关键业务操作，必须由主流程直接完成。

EventBus 仅用于：

- 日志；
- 调试；
- 指标；
- 插件通知；
- 非关键副作用。

### 2.4 消息持久化采用追加写

消息表是唯一事实来源。Session 缓存只是读取优化，不能用旧缓存全量覆盖数据库中的新消息。

### 2.5 明确区分“已接受”和“已送达”

消息进入本地 Outbox，只能称为 `accepted`。

只有外部平台 API 明确返回成功后，才能称为 `delivered`。

---

## 3. 目录结构

```text
cogito/
├── __init__.py
│
├── bus/                              # 入站总线、生命周期事件与 Hook
│   ├── __init__.py
│   ├── inbound.py                    # InboundBus / InboundPort
│   ├── event_bus.py                  # DomainEventBus：只读生命周期事件
│   ├── hooks.py                      # HookPipeline：可修改、拒绝或短路
│   ├── events.py                     # 核心消息数据类
│   └── events_lifecycle.py           # 生命周期事件数据类
│
├── channels/                         # 外部信道适配层
│   ├── __init__.py
│   ├── contract.py                   # Channel Protocol
│   ├── registry.py                   # ChannelRegistry
│   ├── base.py                       # AttachmentStore / 公共工具
│   ├── cli.py                        # CLI 信道
│   ├── telegram.py                   # Telegram 信道
│   ├── qq.py                         # QQ 信道
│   └── ipc_server.py                 # IPC 信道
│
├── loop/                             # Agent 主循环与 Session 调度
│   ├── __init__.py
│   ├── agent_loop.py                 # AgentLoop：消费入站并交给 Router
│   ├── mailbox.py                    # SessionMailboxRouter
│   ├── turn_runner.py                # 单个 Turn 的执行流程
│   ├── config.py                     # AgentLoopConfig / LLMConfig
│   ├── deps.py                       # AgentLoopDeps
│   ├── interrupt.py                  # TurnInterruptController
│   └── handlers.py                   # 流式响应和 Provider 事件处理
│
├── session/                          # 会话和消息持久化
│   ├── __init__.py
│   ├── manager.py                    # SessionManager：缓存与上下文装配
│   ├── store.py                      # SQLite Store
│   └── model.py                      # Session / StoredMessage
│
├── turns/                            # Turn 状态、结果与事务提交
│   ├── __init__.py
│   ├── coordinator.py                # TurnCoordinator
│   ├── result.py                     # TurnResult / TurnTrace
│   └── state.py                      # TurnState / ActiveTurn
│
├── delivery/                         # 统一出站投递
│   ├── __init__.py
│   ├── manager.py                    # DeliveryManager
│   ├── model.py                      # OutboundRequest / DeliveryReceipt
│   ├── retry.py                      # RetryPolicy / RetryScheduler
│   └── outbox.py                     # SQLite OutboxStore
│
├── tools/
│   ├── __init__.py
│   ├── base.py
│   ├── registry.py
│   └── message_push.py               # 向 DeliveryManager 提交消息
│
├── provider.py                       # LLM Provider，后续可拆包
├── config.py                         # 系统配置加载
└── application.py                    # 启动、TaskGroup 与优雅关闭
```

---

## 4. 核心数据模型

## 4.1 消息载荷

消息载荷与信道协议解耦。

```python
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class AttachmentRef:
    id: str
    content_type: str
    size: int
    sha256: str
    local_path: str | None = None
    remote_refs: Mapping[str, str] = field(default_factory=dict)


MessagePart = TextPart | AttachmentRef


@dataclass(frozen=True)
class MessagePayload:
    parts: Sequence[MessagePart]
```

附件在消息总线中只传引用，不直接传输大块二进制数据。

---

## 4.2 入站消息

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    external_message_id: str | None

    session_key: str
    channel: str
    target: str

    payload: MessagePayload

    trace_id: str
    received_at: datetime
    occurred_at: datetime | None = None

    reply_to: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

字段语义：

| 字段 | 含义 |
|---|---|
| `message_id` | Cogito 内部消息 ID |
| `external_message_id` | Telegram/QQ 等平台提供的消息 ID，用于去重 |
| `session_key` | 会话唯一键 |
| `channel` | 来源信道 |
| `target` | 平台中的聊天、群组或终端目标 |
| `trace_id` | 完整处理链路 ID |
| `received_at` | Cogito 接收时间 |
| `occurred_at` | 外部平台事件发生时间 |

推荐的 Session Key 格式：

```text
telegram:<bot-account>:<chat-id>:<thread-id>
qq:<account>:<group-or-user-id>
cli:<profile>:<terminal-id>
ipc:<client-id>:<conversation-id>
```

---

## 4.3 入站控制消息

中断等控制行为也通过核心层处理，不允许 Channel 直接调用内部控制器。

```python
@dataclass(frozen=True)
class InboundControl:
    control_id: str
    kind: Literal["interrupt", "reset_session", "shutdown"]
    session_key: str | None
    channel: str
    trace_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


InboundItem = InboundMessage | InboundControl
```

---

## 4.4 Turn 上下文

```python
@dataclass(frozen=True)
class TurnContext:
    turn_id: str
    trace_id: str
    session_key: str
    trigger_message_id: str | None

    origin: Literal["inbound", "proactive", "system"]
    started_at: datetime
```

---

## 4.5 出站请求

```python
@dataclass(frozen=True)
class OutboundRequest:
    outbound_id: str

    channel: str
    target: str
    payload: MessagePayload

    origin: Literal["reply", "proactive", "tool"]

    trace_id: str
    session_key: str | None = None
    turn_id: str | None = None

    priority: int = 100
    idempotency_key: str | None = None
    created_at: datetime | None = None

    metadata: Mapping[str, Any] = field(default_factory=dict)
```

建议优先级：

```text
紧急系统通知       priority = 10
主动推送           priority = 50
普通被动回复       priority = 100
低优先级后台消息   priority = 150
```

---

## 4.6 投递结果

```python
@dataclass(frozen=True)
class DeliveryReceipt:
    outbound_id: str
    status: Literal[
        "accepted",
        "delivered",
        "retrying",
        "failed",
        "dead",
    ]

    external_message_id: str | None = None
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
```

---

## 5. bus/ — 入站总线、Hook 与生命周期事件

## 5.1 InboundBus

InboundBus 只负责标准入站工作项的排队。

```python
class InboundBus:
    def __init__(self, maxsize: int = 100):
        self._queue: asyncio.Queue[InboundItem] = asyncio.Queue(
            maxsize=maxsize
        )

    async def publish(self, item: InboundItem) -> None:
        await self._queue.put(item)

    async def consume(self) -> InboundItem:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()
```

队列必须有上限，避免 Provider 或工具长时间阻塞后，入站无限堆积。

个人项目默认值可设为：

```python
inbound_queue_size = 100
```

---

## 5.2 HookPipeline

Hook 用于会影响主流程结果的拦截逻辑。

适用场景：

- BeforeTurn；
- BeforeLLM；
- BeforeToolCall；
- BeforeCommit；
- 权限检查；
- 输入清洗；
- 上下文变换；
- 主动跳过或拒绝。

```python
class HookPipeline:
    def register(
        self,
        stage: str,
        handler,
        *,
        priority: int = 100,
    ) -> None:
        ...

    async def run(self, stage: str, context):
        ...
```

Hook 必须具备：

- 确定的执行顺序；
- 明确的优先级；
- 明确的异常策略；
- 可短路或拒绝；
- 不允许隐式并发修改同一个上下文。

---

## 5.3 DomainEventBus

DomainEventBus 只发布不可变生命周期事件。

```python
class DomainEventBus:
    def on(self, event_type, handler) -> Subscription:
        ...

    async def publish(self, event) -> None:
        ...

    def enqueue(self, event) -> None:
        ...
```

规则：

1. Event 不允许被 Handler 修改；
2. Event Handler 不参与核心事务；
3. Event Handler 失败不能破坏 Turn 的已提交状态；
4. 非关键事件可以异步 fanout；
5. 调试或审计事件可持久化，但不是 v1 必需项。

---

## 5.4 生命周期事件

建议事件集合：

```text
InboundReceived
InboundAccepted
InboundDuplicateIgnored

TurnQueued
TurnStarted
TurnCancelRequested
TurnCancelled
TurnFailed

LLMCallStarted
LLMCallCompleted
LLMCallFailed

ToolCallStarted
ToolCallCompleted
ToolCallFailed

TurnCommitting
TurnCommitted

OutboundAccepted
DeliveryStarted
DeliverySucceeded
DeliveryRetryScheduled
DeliveryFailed
DeliveryDead
```

每个事件至少包含：

```python
@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    event_type: str
    occurred_at: datetime

    trace_id: str
    session_key: str | None = None
    turn_id: str | None = None
    message_id: str | None = None
    outbound_id: str | None = None
```

---

## 6. channels/ — 信道适配层

## 6.1 Channel Protocol

```python
from typing import Protocol


class Channel(Protocol):
    name: str

    async def run(self, inbound: "InboundPort") -> None:
        """
        持续监听外部平台事件，并向 InboundPort 提交标准消息。
        """

    async def send(
        self,
        request: OutboundRequest,
    ) -> DeliveryReceipt:
        """
        将标准出站请求发送到外部平台。
        """

    async def close(self) -> None:
        """
        停止监听并释放连接、会话等资源。
        """
```

---

## 6.2 InboundPort

Channel 只依赖最小入站接口。

```python
class InboundPort(Protocol):
    async def publish(self, item: InboundItem) -> None:
        ...
```

如果 Channel 需要附件存储，可通过最小运行上下文传入：

```python
@dataclass(frozen=True)
class ChannelRuntime:
    inbound: InboundPort
    attachment_store: AttachmentStore
```

Channel 不应依赖：

```text
SessionManager
DomainEventBus
HookPipeline
MessagePushTool
TurnInterruptController
AgentLoop
```

---

## 6.3 ChannelRegistry

所有 Channel 统一注册到 ChannelRegistry。

```python
class ChannelRegistry:
    def __init__(self):
        self._channels: dict[str, Channel] = {}

    def register(self, channel: Channel) -> None:
        if channel.name in self._channels:
            raise ValueError(f"channel already registered: {channel.name}")
        self._channels[channel.name] = channel

    def get(self, name: str) -> Channel:
        try:
            return self._channels[name]
        except KeyError as exc:
            raise UnknownChannelError(name) from exc

    def all(self) -> tuple[Channel, ...]:
        return tuple(self._channels.values())
```

不再使用：

```text
bus.subscribe_outbound(channel, callback)
push_tool.register_channel(channel, sender)
```

避免同一个 Channel 存在两套发送注册机制。

---

## 6.4 Channel 工作循环

```text
Channel.run()
  │
  ├── 监听外部长连接、轮询、Webhook 或 stdin
  │
  ├── 将平台消息转换为 InboundMessage
  │
  ├── 将附件保存到 AttachmentStore
  │
  └── inbound.publish(message)
```

收到出站请求时：

```text
DeliveryManager
  │
  └── ChannelRegistry.get(request.channel)
          │
          └── channel.send(request)
                  │
                  └── Telegram API / QQ API / stdout
```

---

## 7. loop/ — Agent 主循环

## 7.1 AgentLoop

AgentLoop 只负责：

- 消费 InboundBus；
- 处理控制消息；
- 将普通消息交给 SessionMailboxRouter；
- 管理启动和关闭。

```python
class AgentLoop:
    def __init__(
        self,
        inbound_bus: InboundBus,
        router: "SessionMailboxRouter",
        interrupt_controller: "TurnInterruptController",
    ):
        self._bus = inbound_bus
        self._router = router
        self._interrupts = interrupt_controller
        self._running = False

    async def run(self) -> None:
        self._running = True

        while self._running:
            item = await self._bus.consume()

            try:
                if isinstance(item, InboundControl):
                    await self._handle_control(item)
                else:
                    await self._router.submit(item)
            finally:
                self._bus.task_done()
```

AgentLoop 不直接执行完整 Turn，避免一个慢请求阻塞所有会话。

---

## 7.2 SessionMailboxRouter

目标：

- 同一个 Session 严格串行；
- 不同 Session 有限并行；
- 保证消息到达顺序；
- 支持 Session 空闲后回收 Worker。

```text
InboundBus
   │
   ▼
SessionMailboxRouter
   ├── session A queue → worker A
   ├── session B queue → worker B
   └── session C queue → worker C
```

建议默认：

```python
max_concurrent_sessions = 4
session_mailbox_size = 20
session_worker_idle_timeout = 300
```

伪代码：

```python
class SessionMailboxRouter:
    def __init__(
        self,
        turn_runner: "TurnRunner",
        max_concurrent_sessions: int = 4,
        mailbox_size: int = 20,
    ):
        self._turn_runner = turn_runner
        self._semaphore = asyncio.Semaphore(max_concurrent_sessions)
        self._mailboxes: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._mailbox_size = mailbox_size

    async def submit(self, msg: InboundMessage) -> None:
        queue = self._mailboxes.get(msg.session_key)

        if queue is None:
            queue = asyncio.Queue(maxsize=self._mailbox_size)
            self._mailboxes[msg.session_key] = queue
            self._workers[msg.session_key] = asyncio.create_task(
                self._run_session_worker(msg.session_key, queue)
            )

        await queue.put(msg)

    async def _run_session_worker(
        self,
        session_key: str,
        queue: asyncio.Queue[InboundMessage],
    ) -> None:
        while True:
            msg = await queue.get()

            try:
                async with self._semaphore:
                    await self._turn_runner.run(msg)
            finally:
                queue.task_done()
```

实际实现中需要补充：

- Worker 空闲超时；
- Worker 异常监控；
- 优雅关闭；
- Session 队列满时的行为；
- Worker 清理逻辑。

---

## 7.3 Busy Session 策略

默认策略为 `QUEUE`。

```python
class BusySessionPolicy(str, Enum):
    QUEUE = "queue"
    INTERRUPT = "interrupt"
    REPLACE_PENDING = "replace_pending"
```

语义：

| 策略 | 行为 |
|---|---|
| `QUEUE` | 新消息在当前 Turn 完成后处理 |
| `INTERRUPT` | 请求取消当前 Turn，再处理新消息 |
| `REPLACE_PENDING` | 保留运行中的 Turn，清除尚未开始的旧消息 |

个人使用默认：

```python
busy_session_policy = BusySessionPolicy.QUEUE
```

特殊命令如 `/stop` 通过 `InboundControl(kind="interrupt")` 请求中断。

---

## 8. TurnRunner — 单个 Turn 的执行流程

```python
class TurnRunner:
    async def run(self, inbound: InboundMessage) -> None:
        context = self._build_context(inbound)

        await self._events.publish(TurnStarted.from_context(context))

        try:
            prepared = await self._hooks.run(
                "before_turn",
                TurnInput(context=context, inbound=inbound),
            )

            history = await self._sessions.build_context(
                inbound.session_key
            )

            result = await self._process(
                context=context,
                inbound=inbound,
                history=history,
            )

            await self._coordinator.finalize(
                context=context,
                inbound=inbound,
                result=result,
            )

        except asyncio.CancelledError:
            await self._handle_cancelled(context)
            raise

        except Exception as exc:
            await self._handle_failed(context, exc)
```

`_process()` 是未来 Phase 管道的替换位置。

当前可以是：

```text
输入预处理
  → 装配上下文
  → 调 LLM
  → 执行 Tool Calls
  → 继续调 LLM
  → 构造 TurnResult
```

未来替换为：

```text
BeforeTurn
  → Context
  → Planning
  → Acting
  → Reflecting
  → Commit
  → AfterTurn
```

---

## 9. Turn 状态与中断

## 9.1 Turn 状态机

```text
queued
  └──▶ running
          ├──▶ cancel_requested ──▶ cancelled
          ├──▶ committing ────────▶ committed ──▶ completed
          └──▶ failed
```

推荐枚举：

```python
class TurnStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMMITTING = "committing"
    COMMITTED = "committed"
    COMPLETED = "completed"
    FAILED = "failed"
```

---

## 9.2 ActiveTurn

```python
@dataclass
class ActiveTurn:
    context: TurnContext
    task: asyncio.Task
    cancel_event: asyncio.Event
    status: TurnStatus
    started_at: datetime
```

---

## 9.3 TurnInterruptController

```python
class TurnInterruptController:
    def __init__(self):
        self._active: dict[str, ActiveTurn] = {}

    def register(self, session_key: str, turn: ActiveTurn) -> None:
        ...

    def unregister(self, session_key: str, turn_id: str) -> None:
        ...

    def request_interrupt(self, session_key: str) -> bool:
        turn = self._active.get(session_key)

        if turn is None:
            return False

        turn.status = TurnStatus.CANCEL_REQUESTED
        turn.cancel_event.set()
        turn.task.cancel()
        return True

    def snapshot(self) -> tuple[ActiveTurn, ...]:
        return tuple(self._active.values())
```

要求：

- Provider 请求必须支持 timeout；
- 工具调用必须支持 timeout；
- 长时间工具应检查 `cancel_event`；
- `CancelledError` 必须继续向上传播；
- 不应把取消当成普通失败吞掉。

---

## 10. session/ — 会话与持久化

## 10.1 数据模型

Session 不持有无限增长的全部消息列表。

```python
@dataclass(frozen=True)
class Session:
    key: str
    metadata: Mapping[str, Any]
    updated_at: datetime
```

上下文由 SessionManager 按需构造：

```python
class SessionManager:
    async def get_or_create(self, key: str) -> Session:
        ...

    async def build_context(
        self,
        key: str,
        *,
        max_messages: int = 50,
        token_budget: int | None = None,
    ) -> list[dict]:
        ...

    async def update_metadata(
        self,
        key: str,
        patch: Mapping[str, Any],
    ) -> None:
        ...
```

---

## 10.2 SQLite 表

```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    trigger_message_id TEXT,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session_started
ON turns(session_key, started_at);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    external_message_id TEXT,
    session_key TEXT NOT NULL,
    turn_id TEXT,
    role TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session_created
ON messages(session_key, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_unique
ON messages(session_key, external_message_id)
WHERE external_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS outbox (
    outbound_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    turn_id TEXT,
    session_key TEXT,
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    origin TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',

    priority INTEGER NOT NULL DEFAULT 100,
    idempotency_key TEXT,

    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    external_message_id TEXT,

    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
ON outbox(status, next_attempt_at, priority, created_at);
```

SQLite 建议：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

个人项目可使用：

- `aiosqlite`；
- 一个进程；
- 一个共享连接或小型连接管理；
- 写操作通过 `asyncio.Lock` 串行化。

---

## 10.3 持久化规则

1. 消息只允许追加；
2. 不使用旧 Session 缓存全量覆盖消息表；
3. 入站消息先去重，再进入 Session Mailbox；
4. Assistant 消息和 Outbox 必须在同一个事务中写入；
5. Outbox 是出站恢复的事实来源；
6. 内存队列只是唤醒和加速机制，不是唯一存储。

---

## 11. TurnCoordinator — 统一提交 Turn

普通回复、主动任务和系统 Turn 最终都通过 TurnCoordinator 提交。

```python
class TurnCoordinator:
    async def finalize(
        self,
        context: TurnContext,
        inbound: InboundMessage | None,
        result: "TurnResult",
    ) -> "TurnCommitResult":
        ...
```

处理流程：

```text
1. 运行 before_commit Hook
2. 验证 TurnResult
3. 开启 SQLite transaction
4. 写入入站消息（如尚未写入）
5. 写入 assistant / tool 消息
6. 写入 Outbox(status=pending)
7. 更新 turns.status = committed
8. 提交事务
9. 发布 TurnCommitted
10. 唤醒 DeliveryManager
```

伪代码：

```python
async def finalize(self, context, inbound, result):
    prepared = await self._hooks.run(
        "before_commit",
        CommitContext(
            turn=context,
            inbound=inbound,
            result=result,
        ),
    )

    async with self._store.transaction() as tx:
        if inbound is not None:
            await tx.insert_inbound_if_absent(inbound)

        await tx.append_messages(prepared.messages)

        for outbound in prepared.outbound_requests:
            await tx.insert_outbox(outbound, status="pending")

        await tx.update_turn_status(
            context.turn_id,
            status="committed",
        )

    await self._events.publish(
        TurnCommitted.from_context(context)
    )

    self._delivery.wakeup()
```

---

## 12. delivery/ — 统一出站投递

## 12.1 DeliveryManager

DeliveryManager 管理：

- Outbox；
- 每个 Channel 的独立队列；
- 每个 Channel 的 Worker；
- 投递超时；
- 错误分类；
- 重试调度；
- 投递状态记录。

```text
                         ┌─ telegram queue → telegram worker → Telegram.send()
Outbox / submit() ──────▶├─ cli queue      → cli worker      → CLI.send()
                         └─ qq queue       → qq worker       → QQ.send()
```

Telegram 卡住不会阻塞 CLI。

---

## 12.2 提交接口

```python
class DeliveryManager:
    async def submit(
        self,
        request: OutboundRequest,
    ) -> DeliveryReceipt:
        """
        将请求写入 Outbox，返回 accepted。
        """

    def wakeup(self) -> None:
        """
        唤醒 Outbox 扫描器。
        """

    async def run(self) -> None:
        """
        恢复并调度 pending/retrying 请求。
        """

    async def close(self, drain: bool = True) -> None:
        ...
```

`submit()` 成功返回：

```python
DeliveryReceipt(
    outbound_id=request.outbound_id,
    status="accepted",
)
```

它不代表外部平台已经送达。

---

## 12.3 Channel Worker

每个 Channel 使用独立 Worker。

```python
async def _channel_worker(
    self,
    channel_name: str,
    queue: asyncio.PriorityQueue,
) -> None:
    channel = self._registry.get(channel_name)

    while True:
        _, _, request = await queue.get()

        try:
            await self._deliver(channel, request)
        finally:
            queue.task_done()
```

优先队列键可使用：

```python
(
    request.priority,
    request.created_at,
    request.outbound_id,
)
```

---

## 12.4 投递错误分类

Channel 应将平台 SDK 异常转换为统一错误。

```python
@dataclass(frozen=True)
class DeliveryError(Exception):
    code: str
    message: str
    retryable: bool
    retry_after: float | None = None
```

可重试：

- 网络超时；
- 连接失败；
- HTTP 429；
- HTTP 5xx；
- 外部平台临时不可用。

不可重试：

- 无效 target；
- Bot 被拉黑；
- 文件格式不支持；
- 附件超过大小限制；
- 权限永久失败；
- 请求参数错误。

---

## 12.5 重试策略

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 300.0
    jitter_ratio: float = 0.2
```

建议退避：

```text
2s → 4s → 8s → 16s → 32s
```

若平台提供 `retry_after`，优先使用平台返回值。

失败后不要在 Channel Worker 中直接长时间 `sleep()`。

正确做法：

1. 更新 Outbox：
   - `status = retrying`
   - `next_attempt_at = ...`
2. Worker 继续处理其他消息；
3. RetryScheduler 到期后重新入队。

达到最大次数：

```text
status = dead
```

系统应提供查询和手动重发接口，但 v1 不需要完整管理后台。

---

## 12.6 投递语义

系统提供：

```text
本地事务：可靠提交
外部投递：at-least-once
```

外部平台发送成功后、本地写 `delivered` 前进程崩溃，仍可能产生重复投递。

降低重复的方法：

- 每条请求携带 `idempotency_key`；
- 平台支持幂等键时直接传递；
- 平台不支持时记录 `external_message_id`；
- 对重复风险可接受，不宣称 exactly-once。

---

## 13. 三种出站路径

所有路径最终统一进入 DeliveryManager。

| 出站方式 | 统一路径 | 适用场景 |
|---|---|---|
| 被动回复 | `TurnRunner → TurnCoordinator → Outbox → DeliveryManager → Channel` | 用户主动聊天的回复 |
| 主动推送 | `ProactiveLoop → TurnCoordinator/DeliveryManager → Outbox → Channel` | 后台任务主动发消息 |
| 工具调用 | `message_push Tool → DeliveryManager → Outbox → Channel` | LLM 自主决定发送消息 |

不再允许：

```text
ProactiveLoop → Channel sender
MessagePushTool → Channel sender
```

主动推送需要避免排队阻塞时，通过：

- 每 Channel 独立队列；
- 优先级；
- 投递超时；
- RetryScheduler；

解决，而不是绕过统一投递系统。

---

## 14. `message_push` 工具权限

模型不能自由向任意 Channel 和 target 发消息。

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    current_session_key: str
    current_channel: str
    current_target: str

    allowed_targets: frozenset[tuple[str, str]]
    allow_cross_session_push: bool = False
```

默认规则：

1. 只能向当前 Session 对应目标发送；
2. 跨 Session 推送必须显式授权；
3. 工具参数中的 channel/target 必须经过校验；
4. 主动系统使用独立内部 Capability；
5. 工具返回 `accepted`，而不是伪装成 `delivered`。

示例：

```python
class MessagePushTool:
    async def execute(
        self,
        args: MessagePushArgs,
        ctx: ToolExecutionContext,
    ) -> dict:
        target = self._resolve_and_authorize(args, ctx)

        receipt = await self._delivery.submit(
            OutboundRequest(
                outbound_id=new_id(),
                channel=target.channel,
                target=target.target,
                payload=args.payload,
                origin="tool",
                trace_id=ctx.trace_id,
                session_key=ctx.current_session_key,
                turn_id=ctx.turn_id,
                idempotency_key=new_id(),
            )
        )

        return {
            "outbound_id": receipt.outbound_id,
            "status": receipt.status,
        }
```

---

## 15. AttachmentStore

AttachmentStore 负责：

- 临时文件保存；
- 内容哈希；
- MIME 类型记录；
- 大小限制；
- 生命周期清理；
- 平台远程文件 ID 缓存；
- 出站重试期间附件可用性。

```python
class AttachmentStore:
    async def put(
        self,
        data: bytes,
        *,
        content_type: str,
    ) -> AttachmentRef:
        ...

    async def open(self, attachment_id: str):
        ...

    async def delete(self, attachment_id: str) -> None:
        ...

    async def cleanup_expired(self) -> int:
        ...
```

建议默认限制：

```text
单附件最大尺寸：20 MB
临时附件保留：24 小时
Outbox 引用中的附件：投递完成或 dead 后再进入清理周期
```

---

## 16. LLM Provider

```python
@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    content: str | None
    tool_calls: tuple["ToolCall", ...]

    finish_reason: str | None
    model: str
    usage: TokenUsage | None = None

    provider_response_id: str | None = None
    thinking: str | None = None
```

Provider 接口：

```python
class LLMProvider:
    async def chat(
        self,
        messages,
        *,
        tools=None,
        model: str,
        max_tokens: int | None = None,
        timeout: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> LLMResponse:
        ...
```

Provider 层负责：

- 请求超时；
- 取消；
- 临时错误映射；
- 流式响应；
- Tool Call 参数解析；
- Token Usage；
- Provider 原始错误转统一异常。

`thinking` 默认不持久化，也不自动进入下一轮上下文。

---

## 17. 消息生命周期全景

```text
外部输入（Telegram / QQ / CLI / IPC）
        │
        ▼
┌─────────────────────┐
│ Channel.run()       │
│ 协议事件标准化       │
└──────────┬──────────┘
           │ InboundMessage
           ▼
┌─────────────────────┐
│ InboundGateway      │
│ 校验 / 去重 / 附件化 │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ InboundBus          │
│ 有界内存队列         │
└──────────┬──────────┘
           ▼
┌──────────────────────────┐
│ SessionMailboxRouter     │
│ 同 Session 串行           │
│ 跨 Session 有限并行       │
└──────────┬───────────────┘
           ▼
┌─────────────────────┐
│ TurnRunner          │
│ Hook / LLM / Tools  │
└──────────┬──────────┘
           │ TurnResult
           ▼
┌─────────────────────┐
│ TurnCoordinator     │
│ SQLite Transaction  │
│ messages + outbox   │
└──────────┬──────────┘
           ▼
┌──────────────────────────┐
│ DeliveryManager          │
│ per-channel queue/worker │
│ timeout / retry          │
└──────────┬───────────────┘
           ▼
┌─────────────────────┐
│ Channel.send()      │
│ 平台 API / stdout   │
└──────────┬──────────┘
           ▼
外部输出
```

旁路生命周期事件：

```text
InboundAccepted
  → TurnQueued
  → TurnStarted
  → LLMCallStarted
  → ToolCallStarted
  → ToolCallCompleted
  → TurnCommitting
  → TurnCommitted
  → OutboundAccepted
  → DeliveryStarted
  → DeliverySucceeded / DeliveryRetryScheduled / DeliveryFailed
```

---

## 18. 入站去重

外部平台可能重复推送同一个事件。

去重键：

```text
(session_key, external_message_id)
```

处理流程：

```text
收到 InboundMessage
  │
  ├── external_message_id 为空
  │      └── 正常接收
  │
  └── external_message_id 非空
         ├── 数据库已存在 → 忽略并发出 InboundDuplicateIgnored
         └── 不存在       → 接收
```

去重写入和入站记录应尽可能使用数据库唯一索引兜底。

---

## 19. 启动与优雅关闭

由 `application.py` 统一管理长期任务。

推荐使用 `asyncio.TaskGroup`：

```python
async def run_application(app: Application) -> None:
    await app.store.open()
    await app.delivery.restore_pending()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(app.agent_loop.run())
        tg.create_task(app.delivery.run())

        for channel in app.channels.all():
            tg.create_task(
                channel.run(
                    ChannelRuntime(
                        inbound=app.inbound_gateway,
                        attachment_store=app.attachment_store,
                    )
                )
            )
```

关闭顺序：

```text
1. 停止接受新的外部输入
2. 停止创建新的 Turn
3. 根据配置等待或取消运行中的 Turn
4. 提交已经完成的 Turn
5. 等待 Outbox Worker 排空，或保留 pending 到下次恢复
6. 关闭 Channel 连接
7. 关闭 SQLite
```

接口建议：

```python
async def start() -> None
async def close(drain: bool = True) -> None
async def wait_closed() -> None
```

长期 Task 异常必须由顶层发现，不能静默退出。

---

## 20. 配置建议

```python
@dataclass(frozen=True)
class AgentLoopConfig:
    inbound_queue_size: int = 100
    session_mailbox_size: int = 20
    max_concurrent_sessions: int = 4
    session_worker_idle_timeout: float = 300.0

    turn_timeout: float = 300.0
    tool_timeout: float = 120.0
    provider_timeout: float = 180.0

    busy_session_policy: str = "queue"


@dataclass(frozen=True)
class DeliveryConfig:
    channel_queue_size: int = 100
    send_timeout: float = 30.0

    retry_max_attempts: int = 5
    retry_base_delay: float = 2.0
    retry_max_delay: float = 300.0

    outbox_poll_interval: float = 1.0
```

这些默认值已足够个人使用，不需要 Redis、Kafka 或分布式锁。

---

## 21. 可观测性

推荐统一结构化日志字段：

```text
event
trace_id
session_key
turn_id
message_id
outbound_id
channel
target
status
duration_ms
attempt
error_code
```

示例：

```python
logger.info(
    "delivery_succeeded",
    extra={
        "trace_id": request.trace_id,
        "turn_id": request.turn_id,
        "outbound_id": request.outbound_id,
        "channel": request.channel,
        "target": request.target,
        "attempt": attempts,
        "duration_ms": duration_ms,
    },
)
```

至少提供以下查询能力：

- 按 `trace_id` 查询完整链路；
- 按 `session_key` 查询历史消息；
- 按 `turn_id` 查询 Turn 状态；
- 查询 pending/retrying/dead Outbox；
- 手动重发 dead 消息；
- 查看当前 active turns。

---

## 22. 未来扩展点

| 扩展 | 接入位置 |
|---|---|
| Phase 管道 | 替换 `TurnRunner._process()` |
| 插件系统 | `HookPipeline + DomainEventBus` |
| 记忆系统 | `before_turn Hook`、`TurnCommitted` 事件 |
| Proactive | 独立 Task，提交标准 Turn 或 OutboundRequest |
| Drift 任务 | Proactive Fetch 阶段分支 |
| 新 Channel | 实现 `Channel Protocol` 并注册到 `ChannelRegistry` |
| Tool Calling | `ToolRegistry` |
| MCP 工具 | `ToolRegistry.register()` |
| 广播 | DeliveryManager 展开为多个 OutboundRequest |
| 消息重试 | `RetryScheduler + Outbox` |
| 消息编辑/撤回 | 扩展 OutboundRequest.action |
| 流式输出 | TurnRunner 产生可选 transient outbound |
| 多 Agent | Router 在 TurnRunner 前增加 AgentSelector |

---

## 23. 不做的事情

Cogito v1 明确不引入：

- Kafka；
- Redis Streams；
- 分布式事务；
- 分布式锁；
- 完整 Event Sourcing；
- 微服务拆分；
- exactly-once 承诺；
- 大规模集群调度；
- 多进程并发写 SQLite。

个人项目采用：

```text
asyncio
+ SQLite WAL
+ aiosqlite
+ 每 Session 一个 Mailbox
+ 每 Channel 一个 Worker
+ Transactional Outbox
```

即可获得足够稳定的行为。

---

## 24. 实施顺序

### 第一阶段：统一运行模型

1. 引入稳定 ID：
   - `message_id`
   - `trace_id`
   - `turn_id`
   - `outbound_id`
2. 将 AgentLoop 改为：
   - 消费 InboundBus；
   - 交给 SessionMailboxRouter。
3. 实现同 Session 串行、跨 Session 有限并行。
4. 精简 Channel Protocol。
5. 引入 ChannelRegistry。
6. 所有队列设置 `maxsize`。
7. 补充 timeout 和优雅关闭。

### 第二阶段：统一出站

1. 新增 DeliveryManager；
2. 每 Channel 独立队列和 Worker；
3. 被动回复改走 DeliveryManager；
4. Proactive 不再直接调用 Channel；
5. `message_push` 不再直接调用 Channel；
6. 增加结构化 DeliveryError；
7. 增加 RetryScheduler。

### 第三阶段：事务与恢复

1. 新增 `turns` 表；
2. 新增 `outbox` 表；
3. Assistant 消息和 Outbox 同事务提交；
4. 启动时恢复 pending/retrying；
5. 增加入站消息去重；
6. 移除 Session 全量覆盖写。

### 第四阶段：扩展能力

1. 拆分 HookPipeline 和 DomainEventBus；
2. 接入 Phase；
3. 接入 Memory；
4. 接入 Proactive；
5. 增加工具 Capability；
6. 完善 AttachmentStore 生命周期。

---

## 25. 核心不变量

实现过程中应始终保持以下不变量：

1. **Channel 不引用 AgentLoop。**
2. **Channel 不直接修改 Session。**
3. **同一 Session 同时最多运行一个 Turn。**
4. **所有需要送达外部平台的消息都进入 Outbox。**
5. **任何组件都不能绕过 DeliveryManager 直接调用 Channel.send。**
6. **Assistant 消息与对应 Outbox 请求在同一个事务中提交。**
7. **Event Handler 失败不能回滚已经提交的 Turn。**
8. **生命周期事件不可变。**
9. **取消必须传播为 `CancelledError`，不能被当成普通异常吞掉。**
10. **内存队列不是事实来源。**
11. **`accepted` 不等于 `delivered`。**
12. **跨 Session 推送默认禁止，必须显式授权。**

---

## 26. 最终架构摘要

```text
Channel
  │
  ▼
InboundGateway
  │
  ▼
InboundBus
  │
  ▼
SessionMailboxRouter
  │
  ▼
TurnRunner
  │
  ▼
TurnCoordinator
  │
  ├── messages
  ├── turns
  └── outbox
          │
          ▼
    DeliveryManager
          │
          ▼
    ChannelRegistry
          │
          ▼
      Channel.send
```

这套结构的重点不是提高极限并发，而是消除多条消息路径之间的语义分裂：

- 入站统一；
- Turn 顺序明确；
- 提交具有事务边界；
- 出站统一；
- 重试统一；
- 权限统一；
- 生命周期统一；
- 重启后可恢复。

对于个人 Agent，这一规模已经足够支撑 CLI、Telegram、QQ、记忆、Phase、工具调用和 Proactive，同时避免引入不必要的分布式复杂度。
