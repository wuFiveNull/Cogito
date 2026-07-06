# Cogito-Agent `PersistencePhase` 最终实现规范

> 文档状态：最终版  
> 适用范围：Cogito-Agent Python 3.12+ 异步运行时、SQLite 3.51.3+  
> 目标读者：架构师、后端工程师、实现型 AI、测试工程师  
> 核心结论：`PersistencePhase` 是一次 Agent Turn 的唯一领域持久化事务边界。它把 `AgentLoopPhase` 和 `KnowledgeExtractionPhase` 已经产生的确定性结果，以原子、幂等、可审计、可恢复的方式写入 SQLite；它不生成回答、不重新检索、不重新执行工具、不直接发布 MessageBus 消息。

---

## 1. 最终设计结论

`PersistencePhase` 必须实现为一个**事务编排器**，而不是一个包含 SQL、业务判断和外部调用的巨型方法。

最终职责链如下：

```text
TurnContext
  │
  ├─ 校验运行状态
  ├─ 清洗可持久化内容
  ├─ 构建不可变 PersistencePlan
  ├─ 在写事务外准备 Embedding
  ├─ 开启 SQLite BEGIN IMMEDIATE
  ├─ 检查 request_id 幂等记录
  ├─ 校验 Session 所有权与版本
  ├─ 分配事件序号
  ├─ 写用户、工具、助手事件
  ├─ 更新 Session 与 Session Summary
  ├─ 应用偏好候选
  ├─ 应用长期记忆候选
  ├─ 写候选处理审计
  ├─ 写 Turn Commit 记录
  ├─ COMMIT
  └─ 更新 TurnContext.persistence_outcome
```

必须满足：

1. 同一 `request_id` 重放不会重复写入。
2. 任意一步失败时，整个 Turn 的领域写入全部回滚。
3. SQLite 写事务中不得等待 LLM、网络工具、文件下载或远程 Embedding。
4. `ctx.persistence_completed` 只能在已确认提交成功或确认等价幂等重放后设置为 `True`。
5. 所有候选必须产生明确处理结果，不能静默丢弃。
6. `events`、`memories`、`trace_events` 保持核心事实表地位；新增表只承载 Session、幂等、审计和恢复控制语义。

---

## 2. Pipeline 位置与边界

```text
AgentLoopPhase
    │
    │ output_text / tool_records / usage
    ▼
KnowledgeExtractionPhase
    │
    │ preference_candidates / memory_candidates / summary_candidate
    ▼
PersistencePhase
    │
    │ PersistenceOutcome
    ▼
TurnFinalizePhase
    │
    ▼
TurnResult
```

### 2.1 输入前置条件

进入 `PersistencePhase` 时必须满足：

```python
ctx.turn_id is not None
ctx.started_at is not None
ctx.status is TurnStatus.RUNNING
ctx.request.request_id.strip() != ""
ctx.request.session_id.strip() != ""
ctx.request.actor_id.strip() != ""
ctx.output_text is not None
ctx.error is None
ctx.persistence_completed is False
ctx.persistence_outcome is None
```

以下数据允许为空：

```python
ctx.tool_records == []
ctx.preference_candidates == []
ctx.memory_candidates == []
ctx.summary_candidate is None
```

### 2.2 成功后置条件

成功返回时必须满足：

```python
ctx.persistence_completed is True
ctx.persistence_outcome is not None
ctx.persistence_outcome.commit_id != ""
ctx.persistence_outcome.session_version >= 1
ctx.metadata["persistence"]["commit_id"] == ctx.persistence_outcome.commit_id
```

成功包括两种情况：

```text
正常提交
等价幂等重放
```

### 2.3 明确禁止

`PersistencePhase` 不得：

- 调用模型生成或修改最终回答；
- 重新进行关键词、向量或记忆检索；
- 重新执行工具；
- 修改已完成的 `output_text`；
- 直接发布 MessageBus、WebSocket 或 Channel 消息；
- 在 Repository 内部各自提交事务；
- 把数据库连接、ORM Session 或具体 SQLite Cursor 暴露给 Domain；
- 在 SQLite 写事务中调用外部服务；
- 捕获异常后伪造成功结果；
- 将完整 Prompt、密钥、Token、Cookie 或未经清洗的工具数据写入日志。

---

## 3. 与现有 SQLite 数据模型的映射

### 3.1 核心业务表保持不变

```text
trace_events
    保存运行步骤、模型调用、工具调用、记忆检索和决策链路

events
    保存用户消息、工具请求、工具结果和 Agent 回复

memories
    保存偏好、事实、规则和重要历史事件

memories_fts
    memories 的可重建全文索引
```

### 3.2 为什么需要增加控制表

现有三张核心业务表不能完整表达以下语义：

- 同一 `request_id` 是否已经完整提交；
- 幂等重放时原提交结果是什么；
- Session 的消息序号和乐观版本；
- Session Summary 的版本；
- 候选为何被应用、去重、忽略或拒绝；
- Embedding 失败后的可靠补偿任务。

因此最终实现增加四张控制表：

```text
sessions
turn_commits
candidate_write_audits
embedding_jobs
```

这些表不是新的用户事实源：

- `events` 仍是原始对话和工具事实源；
- `memories` 仍是长期记忆事实源；
- `trace_events` 仍是运行审计事实源；
- 控制表只保证事务、幂等、并发和恢复正确性。

---

## 4. 最终数据库迁移

以下迁移基于既有 `trace_events`、`events`、`memories` 和 `memories_fts`。

### 4.1 为事件增加 Turn 关联字段

```sql
ALTER TABLE events ADD COLUMN request_id TEXT;
ALTER TABLE events ADD COLUMN turn_id TEXT;

CREATE INDEX idx_events_request
ON events(user_id, request_id)
WHERE request_id IS NOT NULL;

CREATE INDEX idx_events_turn_seq
ON events(turn_id, seq_no)
WHERE turn_id IS NOT NULL;

CREATE UNIQUE INDEX idx_events_turn_user_message
ON events(turn_id)
WHERE event_type = 'user_message'
  AND turn_id IS NOT NULL;

CREATE UNIQUE INDEX idx_events_turn_assistant_message
ON events(turn_id)
WHERE event_type = 'assistant_message'
  AND turn_id IS NOT NULL;
```

### 4.2 为 Trace 增加请求关联字段

```sql
ALTER TABLE trace_events ADD COLUMN request_id TEXT;
ALTER TABLE trace_events ADD COLUMN turn_id TEXT;

CREATE INDEX idx_trace_events_request
ON trace_events(user_id, request_id, started_at)
WHERE request_id IS NOT NULL;

CREATE INDEX idx_trace_events_turn
ON trace_events(turn_id, started_at)
WHERE turn_id IS NOT NULL;
```

### 4.3 Session 控制表

```sql
CREATE TABLE sessions (
    session_id              TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,

    version                 INTEGER NOT NULL DEFAULT 0
                            CHECK (version >= 0),

    next_seq_no             INTEGER NOT NULL DEFAULT 1
                            CHECK (next_seq_no >= 1),

    summary_text            TEXT,
    summary_version         INTEGER NOT NULL DEFAULT 0
                            CHECK (summary_version >= 0),
    summary_updated_at      TEXT,

    last_turn_id            TEXT,
    last_request_id         TEXT,
    last_message_at         TEXT,

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;

CREATE INDEX idx_sessions_user_updated
ON sessions(user_id, updated_at DESC);

CREATE TRIGGER trg_sessions_touch_updated_at
AFTER UPDATE ON sessions
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE sessions
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE session_id = NEW.session_id;
END;
```

### 4.4 Turn 幂等提交表

```sql
CREATE TABLE turn_commits (
    commit_id               TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    session_id              TEXT NOT NULL
                            REFERENCES sessions(session_id),
    request_id              TEXT NOT NULL,
    turn_id                 TEXT NOT NULL,

    commit_fingerprint      TEXT NOT NULL,

    user_event_id           TEXT NOT NULL
                            REFERENCES events(id),
    assistant_event_id      TEXT NOT NULL
                            REFERENCES events(id),

    session_version         INTEGER NOT NULL
                            CHECK (session_version >= 1),

    outcome_json            TEXT NOT NULL
                            CHECK (
                                json_valid(outcome_json)
                                AND json_type(outcome_json) = 'object'
                            ),

    persistence_span_id     TEXT REFERENCES trace_events(id),

    committed_at            TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),

    UNIQUE(user_id, request_id),
    UNIQUE(turn_id)
) STRICT;

CREATE INDEX idx_turn_commits_session
ON turn_commits(session_id, committed_at DESC);
```

### 4.5 候选写入审计表

```sql
CREATE TABLE candidate_write_audits (
    id                      TEXT PRIMARY KEY,
    commit_id               TEXT NOT NULL
                            REFERENCES turn_commits(commit_id)
                            ON DELETE CASCADE,
    user_id                 TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    turn_id                 TEXT NOT NULL,

    candidate_id            TEXT NOT NULL,
    candidate_type          TEXT NOT NULL
                            CHECK (
                                candidate_type IN (
                                    'preference',
                                    'memory',
                                    'summary'
                                )
                            ),
    candidate_key           TEXT,
    requested_operation     TEXT NOT NULL,
    result_status           TEXT NOT NULL
                            CHECK (
                                result_status IN (
                                    'applied_insert',
                                    'applied_update',
                                    'applied_delete',
                                    'superseded',
                                    'deduplicated',
                                    'tentative',
                                    'ignored',
                                    'rejected'
                                )
                            ),
    target_record_id        TEXT,
    reason_code             TEXT,
    confidence              REAL,
    importance              REAL,
    source_event_ids_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(source_event_ids_json)
                                AND json_type(source_event_ids_json) = 'array'
                            ),
    metadata_json           TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(metadata_json)),
    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),

    UNIQUE(commit_id, candidate_id)
) STRICT;

CREATE INDEX idx_candidate_audits_turn
ON candidate_write_audits(turn_id, candidate_type);
```

### 4.6 Embedding 补偿任务表

```sql
CREATE TABLE embedding_jobs (
    id                      TEXT PRIMARY KEY,
    memory_id               TEXT NOT NULL
                            REFERENCES memories(id)
                            ON DELETE CASCADE,
    embedding_model         TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (
                                status IN (
                                    'pending',
                                    'processing',
                                    'done',
                                    'failed'
                                )
                            ),
    attempts                INTEGER NOT NULL DEFAULT 0
                            CHECK (attempts >= 0),
    last_error              TEXT,
    available_at            TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),

    UNIQUE(memory_id, embedding_model)
) STRICT;

CREATE INDEX idx_embedding_jobs_pending
ON embedding_jobs(status, available_at)
WHERE status IN ('pending', 'failed');
```

### 4.7 Schema 版本

迁移完成后设置明确版本：

```sql
PRAGMA user_version = 2;
```

应用启动时必须检查版本，禁止通过运行时猜测列是否存在。

---

## 5. 领域模型必须补全

初始框架中的候选模型字段不足以支持最终持久化。必须升级为显式、不可变的领域对象。

### 5.1 候选操作

```python
from enum import StrEnum


class CandidateOperation(StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    IGNORE = "ignore"
    TENTATIVE = "tentative"
```

### 5.2 偏好候选

```python
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PreferenceCandidate:
    candidate_id: str
    key: str
    value: object | None
    content: str
    operation: CandidateOperation
    confidence: float
    importance: float = 0.5
    source_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### 5.3 记忆候选

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class MemoryType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    RULE = "rule"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    candidate_id: str
    memory_type: MemoryType
    memory_key: str
    content: str
    value: object
    operation: CandidateOperation
    confidence: float
    importance: float
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    source_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### 5.4 摘要候选

```python
@dataclass(frozen=True, slots=True)
class SummaryCandidate:
    candidate_id: str
    content: str
    confidence: float
    expected_version: int | None
    source_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### 5.5 可持久化工具记录

原 `ToolExecutionRecord` 只包含结果摘要，不能完整构造 `tool_request` 和 `tool_result` 事件。最终模型必须保留经过清洗的输入输出和稳定顺序。

```python
@dataclass(frozen=True, slots=True)
class PersistableToolRecord:
    call_id: str
    ordinal: int
    tool_name: str
    succeeded: bool
    started_at: datetime
    completed_at: datetime
    duration_ms: int | None
    safe_arguments: Mapping[str, object]
    safe_result: Mapping[str, object] | None
    error_code: str | None = None
    safe_error_message: str | None = None
```

禁止把 SDK 响应对象直接放进该模型。

---

## 6. `TurnContext` 最终字段

核心生命周期状态必须正式建模，不得长期放在 `metadata`。

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class TurnContext:
    # 已有字段省略

    current_span_id: str | None = None

    session: SessionSnapshot | None = None
    recent_messages: list[ConversationEvent] = field(default_factory=list)
    session_summary: SessionSummary | None = None

    tool_records: list[PersistableToolRecord] = field(default_factory=list)

    preference_candidates: list[PreferenceCandidate] = field(default_factory=list)
    memory_candidates: list[MemoryCandidate] = field(default_factory=list)
    summary_candidate: SummaryCandidate | None = None

    persistence_completed: bool = False
    persistence_outcome: PersistenceOutcome | None = None
```

`current_span_id` 由 Kernel 的 Trace 生命周期组件在 Phase 执行前设置；若 Trace Adapter 不可用，可以为空，数据库外键字段保持 `NULL`。

---

## 7. 不可变持久化计划

Repository 不得接收整个 `TurnContext`。`TurnContext` 必须先转换为一个不可变的 `PersistencePlan`。

### 7.1 事件草稿

```python
@dataclass(frozen=True, slots=True)
class EventDraft:
    event_id: str
    user_id: str
    session_id: str
    request_id: str
    turn_id: str
    role: str
    event_type: str
    content: str
    content_json: Mapping[str, object]
    extraction_status: str
    logical_order: int
    created_at: datetime
```

### 7.2 Embedding 草稿

```python
@dataclass(frozen=True, slots=True)
class PreparedEmbedding:
    candidate_id: str
    model: str
    dimensions: int
    blob: bytes
    format: str = "float32-le"
```

### 7.3 持久化计划

```python
@dataclass(frozen=True, slots=True)
class PersistencePlan:
    commit_id: str
    turn_id: str
    request_id: str
    user_id: str
    session_id: str
    persistence_span_id: str | None

    expected_session_version: int | None
    expected_summary_version: int | None

    events: tuple[EventDraft, ...]
    preference_candidates: tuple[PreferenceCandidate, ...]
    memory_candidates: tuple[MemoryCandidate, ...]
    summary_candidate: SummaryCandidate | None
    embeddings: tuple[PreparedEmbedding, ...]

    usage: UsageSummary
    started_at: datetime
    persistence_started_at: datetime
    commit_fingerprint: str
```

计划必须满足：

- ID 在重试循环外生成；
- 同一执行过程的数据库重试复用同一个 Plan；
- 计划字段不可变；
- 指纹不包含数据库生成的 Session 版本和事件序号；
- 指纹包含所有会影响领域写入的规范化内容。

### 7.4 持久化结果

```python
@dataclass(frozen=True, slots=True)
class CandidateWriteOutcome:
    candidate_id: str
    candidate_type: str
    candidate_key: str | None
    status: str
    record_id: str | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class PersistenceOutcome:
    commit_id: str
    turn_id: str
    request_id: str
    session_id: str
    committed_at: datetime
    session_version: int
    summary_version: int
    idempotent_replay: bool
    user_event_id: str
    assistant_event_id: str
    tool_event_ids: tuple[str, ...]
    candidate_outcomes: tuple[CandidateWriteOutcome, ...]
    embedding_job_count: int
```

---

## 8. 目录结构

```text
cogito_agent/
├── runtime/
│   ├── context.py
│   ├── errors.py
│   ├── persistence/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── planner.py
│   │   ├── sanitizer.py
│   │   ├── fingerprint.py
│   │   ├── preference_policy.py
│   │   ├── memory_policy.py
│   │   ├── retry.py
│   │   └── commit_recovery.py
│   └── phases/
│       └── persistence.py
│
├── domain/
│   ├── sessions.py
│   ├── messages.py
│   ├── preferences.py
│   ├── memory.py
│   ├── summaries.py
│   └── usage.py
│
├── ports/
│   ├── clock.py
│   ├── ids.py
│   ├── embeddings.py
│   ├── repositories.py
│   └── unit_of_work.py
│
├── infrastructure/
│   └── sqlite/
│       ├── connection.py
│       ├── unit_of_work.py
│       ├── repositories/
│       │   ├── sessions.py
│       │   ├── events.py
│       │   ├── memories.py
│       │   ├── turn_commits.py
│       │   ├── candidate_audits.py
│       │   └── embedding_jobs.py
│       └── migrations/
│           └── 002_persistence_control.sql
│
├── application/
│   └── embedding_worker.py
│
└── tests/
    ├── unit/runtime/phases/test_persistence.py
    ├── unit/runtime/persistence/test_planner.py
    ├── unit/runtime/persistence/test_preference_policy.py
    ├── unit/runtime/persistence/test_memory_policy.py
    ├── integration/sqlite/test_atomic_turn_commit.py
    ├── integration/sqlite/test_idempotent_replay.py
    ├── integration/sqlite/test_session_concurrency.py
    ├── integration/sqlite/test_memory_supersession.py
    ├── integration/sqlite/test_fts_consistency.py
    └── architecture/test_persistence_boundaries.py
```

---

## 9. Port 设计

### 9.1 Unit of Work

```python
from typing import Protocol, Self


class UnitOfWorkPort(Protocol):
    sessions: SessionRepositoryPort
    events: EventRepositoryPort
    memories: MemoryRepositoryPort
    turn_commits: TurnCommitRepositoryPort
    candidate_audits: CandidateAuditRepositoryPort
    embedding_jobs: EmbeddingJobRepositoryPort

    async def __aenter__(self) -> Self:
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        ...

    async def commit(self) -> None:
        ...

    async def rollback(self) -> None:
        ...
```

### 9.2 UoW Factory

```python
class UnitOfWorkFactoryPort(Protocol):
    def create(self) -> UnitOfWorkPort:
        ...
```

每次重试必须创建新的 UoW 和新的数据库连接事务状态。

### 9.3 Session Repository

```python
class SessionRepositoryPort(Protocol):
    async def create_if_absent(
        self,
        *,
        session_id: str,
        user_id: str,
        now: datetime,
    ) -> None:
        ...

    async def get_for_write(
        self,
        *,
        session_id: str,
    ) -> SessionSnapshot | None:
        ...

    async def advance(
        self,
        *,
        session_id: str,
        expected_version: int,
        consumed_sequences: int,
        last_turn_id: str,
        last_request_id: str,
        last_message_at: datetime,
    ) -> SessionSnapshot:
        ...

    async def update_summary(
        self,
        *,
        session_id: str,
        content: str,
        expected_summary_version: int,
        now: datetime,
    ) -> SessionSnapshot:
        ...
```

### 9.4 Event Repository

```python
class EventRepositoryPort(Protocol):
    async def add_many(
        self,
        events: tuple[PersistedEvent, ...],
    ) -> None:
        ...

    async def get_by_id(self, event_id: str) -> PersistedEvent | None:
        ...
```

### 9.5 Memory Repository

```python
class MemoryRepositoryPort(Protocol):
    async def get_active_by_key(
        self,
        *,
        user_id: str,
        memory_key: str,
    ) -> LongTermMemory | None:
        ...

    async def insert(self, memory: LongTermMemory) -> None:
        ...

    async def update_reinforcement(
        self,
        *,
        memory_id: str,
        confidence: float,
        importance: float,
        source_event_ids: tuple[str, ...],
        updated_by_span_id: str | None,
    ) -> None:
        ...

    async def mark_superseded(
        self,
        *,
        memory_id: str,
        valid_until: datetime,
        updated_by_span_id: str | None,
    ) -> None:
        ...

    async def soft_delete(
        self,
        *,
        memory_id: str,
        updated_by_span_id: str | None,
    ) -> None:
        ...
```

### 9.6 Turn Commit Repository

```python
class TurnCommitRepositoryPort(Protocol):
    async def get_by_request(
        self,
        *,
        user_id: str,
        request_id: str,
    ) -> TurnCommitRecord | None:
        ...

    async def add(self, record: TurnCommitRecord) -> None:
        ...
```

### 9.7 Candidate Audit Repository

```python
class CandidateAuditRepositoryPort(Protocol):
    async def add_many(
        self,
        audits: tuple[CandidateWriteAudit, ...],
    ) -> None:
        ...
```

### 9.8 Embedding Job Repository

```python
class EmbeddingJobRepositoryPort(Protocol):
    async def add_many(
        self,
        jobs: tuple[EmbeddingJob, ...],
    ) -> None:
        ...
```

### 9.9 Embedding Port

```python
@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    model: str
    dimensions: int
    values: tuple[float, ...]


class EmbeddingPort(Protocol):
    async def embed_many(
        self,
        texts: tuple[str, ...],
    ) -> tuple[EmbeddingVector, ...]:
        ...
```

Embedding Port 只负责计算，不负责数据库写入。

---

## 10. SQLite Unit of Work

### 10.1 连接初始化

每个连接执行：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

### 10.2 事务开启方式

最终实现使用：

```sql
BEGIN IMMEDIATE;
```

原因：

- 在进入写入流程时提前获得 Reserved Lock；
- 防止两个 Worker 同时读取相同 `next_seq_no` 后再竞争写入；
- 写锁冲突在事务开始处暴露，便于有限重试；
- 事务内不包含外部 I/O，因此锁持有时间可控。

### 10.3 参考实现

```python
class SQLiteUnitOfWork:
    def __init__(self, connection_factory: SQLiteConnectionFactory) -> None:
        self._connection_factory = connection_factory
        self._connection = None
        self._committed = False

    async def __aenter__(self) -> "SQLiteUnitOfWork":
        self._connection = await self._connection_factory.open()
        await self._connection.execute("BEGIN IMMEDIATE")

        self.sessions = SQLiteSessionRepository(self._connection)
        self.events = SQLiteEventRepository(self._connection)
        self.memories = SQLiteMemoryRepository(self._connection)
        self.turn_commits = SQLiteTurnCommitRepository(self._connection)
        self.candidate_audits = SQLiteCandidateAuditRepository(self._connection)
        self.embedding_jobs = SQLiteEmbeddingJobRepository(self._connection)
        return self

    async def commit(self) -> None:
        if self._connection is None:
            raise RuntimeError("unit of work is not active")
        await self._connection.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self._connection is not None and not self._committed:
            await self._connection.rollback()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc is not None and not self._committed:
                await self.rollback()
        finally:
            if self._connection is not None:
                await self._connection.close()
```

Repository 内不得调用 `commit()`。

---

## 11. PersistencePlan 构建

### 11.1 构建顺序

`PersistencePlanBuilder` 按以下顺序执行纯内存转换：

1. 生成稳定的 `commit_id`、事件 ID 和候选目标 ID；
2. 将当前请求转为用户事件草稿；
3. 按 `tool_records.ordinal` 排序；
4. 每个工具生成 `tool_request` 和 `tool_result` 或 `tool_error` 草稿；
5. 将 `output_text` 转为助手事件草稿；
6. 解析候选来源引用并映射到事件 ID；
7. 规范化偏好 Key、记忆 Key、JSON 和文本；
8. 计算内容 Hash、候选 Fingerprint 和 Turn Commit Fingerprint；
9. 生成不可变 Plan。

### 11.2 事件逻辑顺序

```text
logical_order = 0      用户消息
logical_order = 10     第 1 个 tool_request
logical_order = 11     第 1 个 tool_result/tool_error
logical_order = 20     第 2 个 tool_request
logical_order = 21     第 2 个 tool_result/tool_error
...
logical_order = 10000  Agent 最终回复
```

数据库 `seq_no` 在事务内基于 `sessions.next_seq_no` 分配。

### 11.3 `extraction_status`

```text
user_message       → pending
assistant_message  → pending
tool_request       → ignored
tool_result        → ignored
tool_error         → ignored
```

工具事件不参与用户长期记忆批量提取，除非后续明确增加工具事实提取策略。

---

## 12. 清洗与序列化

### 12.1 `PersistenceSanitizer`

清洗器负责：

- 删除密钥、Token、Cookie、Authorization Header；
- 对工具输入输出执行字段白名单或敏感字段遮盖；
- 限制单字段长度；
- 将不可序列化对象拒绝或转换为安全摘要；
- 规范化 Unicode 和换行；
- 保证 JSON 使用稳定键顺序；
- 大型工具结果只保存引用、摘要、大小和 Hash。

### 12.2 大型结果策略

```text
<= 100 KB
    内联写入 events.content_json

> 100 KB
    外部文件保存
    events.content_json 只保存：
        storage_uri
        sha256
        media_type
        size_bytes
        summary
```

外部文件必须在进入 SQLite 写事务前完成原子落盘，文件名使用内容 Hash；数据库事务只写引用。

### 12.3 JSON 规范

统一使用：

```python
json.dumps(
    value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

该规范同时用于：

- 数据库存储；
- Fingerprint 输入；
- 测试快照。

---

## 13. Embedding 准备

### 13.1 事务外执行

Embedding 必须在 `BEGIN IMMEDIATE` 之前完成：

```text
候选规范化
→ 选择需要 Embedding 的候选
→ 调用 EmbeddingPort
→ Float32 L2 归一化
→ 编码为 little-endian BLOB
→ 放入 PersistencePlan
→ 开启数据库写事务
```

### 13.2 不阻塞核心消息持久化

最终策略：

- Embedding 成功：随 Memory 在同一事务写入 BLOB；
- Embedding 失败：Memory 仍可写入，事务内同时写 `embedding_jobs`；
- Embedding 失败原因只保存安全错误码，不保存供应商密钥或完整请求；
- 后续 Worker 根据 `embedding_jobs` 补写 `memories.embedding`；
- `memories` 始终是事实源，Embedding 是可重建派生数据。

### 13.3 哪些候选需要 Embedding

```text
INSERT memory      → 需要
UPDATE memory      → 需要
INSERT preference  → 需要
UPDATE preference  → 需要
DELETE              → 不需要
IGNORE              → 不需要
TENTATIVE           → 不进入 active memories，不需要
```

允许预计算后在事务内判定为重复，从而丢弃未使用的向量；这是为了避免在写锁内调用外部服务。

---

## 14. 幂等设计

### 14.1 幂等键

```text
(user_id, request_id)
```

`request_id` 必须由入口层稳定生成，并在 Worker 重试、消息重投和进程恢复时保持不变。

### 14.2 Commit Fingerprint

指纹输入：

```text
schema_version
user_id
session_id
request_id
normalized_user_text
normalized_output_text
ordered_tool_record_digest
ordered_candidate_digest
summary_digest
usage_digest
```

算法：

```python
sha256(canonical_json(payload).encode("utf-8")).hexdigest()
```

不包含：

- `turn_id`；
- `commit_id`；
- 事件随机 ID；
- 数据库序号；
- 数据库提交时间。

### 14.3 等价重放

事务开始后首先查询：

```sql
SELECT *
FROM turn_commits
WHERE user_id = :user_id
  AND request_id = :request_id;
```

处理规则：

```text
不存在
    → 继续正常持久化

存在且 fingerprint 相同
    → 解析 outcome_json
    → 返回 idempotent_replay=True
    → 不再写任何业务数据

存在但 fingerprint 不同
    → 抛 IdempotencyConflictError
```

同一幂等键对应不同业务内容属于严重调用方错误，禁止覆盖旧数据。

---

## 15. Session 并发控制

### 15.1 Session 创建

```sql
INSERT INTO sessions (
    session_id,
    user_id,
    version,
    next_seq_no
)
VALUES (
    :session_id,
    :user_id,
    0,
    1
)
ON CONFLICT(session_id) DO NOTHING;
```

随后读取并校验：

```text
session.user_id == plan.user_id
```

不相等时抛 `SessionOwnershipError`。

### 15.2 乐观版本

StateLoad 阶段读取的 `ctx.session.version` 写入 `expected_session_version`。

事务内执行：

```sql
UPDATE sessions
SET version = version + 1,
    next_seq_no = next_seq_no + :event_count,
    last_turn_id = :turn_id,
    last_request_id = :request_id,
    last_message_at = :last_message_at
WHERE session_id = :session_id
  AND version = :expected_version
RETURNING *;
```

受影响行数为 0：

- 重新读取 Session；
- 如果只是序号和版本变化，使用同一 Plan 重新进入完整事务；
- 不重新调用模型、工具或知识抽取；
- 超出有限重试后抛 `OptimisticConcurrencyError`。

### 15.3 事件序号

事务内读取更新前的：

```text
base_seq_no = session.next_seq_no
```

按 `logical_order` 排序后分配：

```text
base_seq_no
base_seq_no + 1
base_seq_no + 2
...
```

唯一索引 `UNIQUE(user_id, session_id, seq_no)` 是最终防线。

---

## 16. Session Summary 规则

### 16.1 写入位置

Session Summary 写入 `sessions.summary_text`，版本写入 `summary_version`。

### 16.2 更新规则

```text
summary_candidate is None
    → 不更新

confidence 低于策略阈值
    → 写 candidate_write_audits(rejected)

expected_version 与当前 summary_version 不一致
    → 抛 SummaryConcurrencyError，触发事务重试

内容与当前摘要规范化后相同
    → 写 deduplicated 审计，不增加 summary_version

内容不同
    → 更新 summary_text
    → summary_version + 1
    → 写 applied_update 审计
```

摘要候选不得在 PersistencePhase 内重新调用模型合并。冲突时只能重试或失败，合并应由上游重新产生候选。

---

## 17. 偏好候选写入策略

偏好统一写入 `memories`：

```text
memory_type = 'preference'
memory_key  = 'preference.' + normalized_key
```

### 17.1 Key 规范化

```text
去除首尾空白
Unicode NFKC
转小写英文
空格和连续点转换为单点
仅允许 [a-z0-9._-] 与受控 Unicode 字符
禁止空 Key
最大 200 字符
```

### 17.2 INSERT

```text
不存在 active key
    → 插入 active memory
    → applied_insert

存在且内容/value 等价
    → 合并 source_event_ids
    → confidence = min(1.0, max(old, new) + reinforcement_bonus)
    → importance = max(old, new)
    → deduplicated

存在但内容不同
    → 旧记录 superseded
    → 插入新记录，supersedes_id 指向旧记录
    → superseded
```

### 17.3 UPDATE

```text
存在 active key
    → 按替代语义执行

不存在 active key
    → 策略允许 upsert 时执行 INSERT
    → 否则 rejected
```

最终配置建议允许 upsert，因为用户偏好抽取可能无法知道数据库当前状态。

### 17.4 DELETE

```sql
UPDATE memories
SET status = 'deleted',
    valid_until = COALESCE(valid_until, :now),
    updated_by_span_id = :span_id
WHERE id = :memory_id
  AND status = 'active';
```

不存在 active key 时记录 `deduplicated`，表示目标状态已经满足。

### 17.5 IGNORE

不修改 `memories`，只写审计：

```text
result_status = ignored
```

### 17.6 TENTATIVE

不写入 active `memories`，只在 `candidate_write_audits` 中保留：

```text
result_status = tentative
```

后续确认流程可以基于审计记录创建新的明确命令，不得直接修改原审计记录。

---

## 18. 长期记忆候选写入策略

### 18.1 自然键

所有长期记忆候选必须有 `memory_key`。

```text
fact        → residence.city
preference  → preference.restaurant.ambience
rule        → rule.payment.require_confirmation
event       → event.restaurant_feedback.2026-06-24.<stable_suffix>
```

事件型记忆 Key 必须包含稳定唯一后缀，避免把不同历史事件错误合并。

### 18.2 等价判断

等价判断不调用 LLM，使用确定性规则：

```text
memory_type 相同
memory_key 相同
canonical value_json 相同
normalized content 相同
valid_from/valid_until 相同
```

### 18.3 强化重复记忆

```python
new_confidence = min(
    1.0,
    max(old.confidence, candidate.confidence) + 0.05,
)
new_importance = max(old.importance, candidate.importance)
source_event_ids = sorted(set(old_sources) | set(new_sources))
```

强化不会创建新 active 记录。

### 18.4 替代旧记忆

固定顺序：

```text
1. UPDATE old SET status='superseded'
2. INSERT new active memory
3. new.supersedes_id = old.id
4. 写 candidate audit
```

该顺序保证部分唯一索引 `idx_memories_active_key` 不冲突。

### 18.5 有效期

写入前校验：

```text
valid_until is None
或 valid_from is None
或 valid_until > valid_from
```

无效时间范围直接 `rejected`，不允许静默修正。

---

## 19. 事务内固定写入顺序

事务内顺序不得由 Repository 调用顺序偶然决定，必须固定如下：

### Step 1：幂等检查

```python
existing = await uow.turn_commits.get_by_request(
    user_id=plan.user_id,
    request_id=plan.request_id,
)
```

### Step 2：创建并校验 Session

```text
create_if_absent
get_for_write
校验 user_id
校验 expected version
```

### Step 3：分配事件序号

基于 `next_seq_no` 和 Plan 的事件数量生成 `PersistedEvent`。

### Step 4：批量写事件

顺序：

```text
user_message
每个 tool_request
每个 tool_result/tool_error
assistant_message
```

### Step 5：推进 Session

原子更新：

```text
version
next_seq_no
last_turn_id
last_request_id
last_message_at
```

### Step 6：应用 Session Summary

根据 `summary_version` 执行确定性更新。

### Step 7：应用偏好

按规范化 `memory_key` 排序，保持稳定锁和写入顺序。

### Step 8：应用记忆

按 `(memory_type, memory_key, candidate_id)` 排序。

### Step 9：写 Embedding Job

仅为本次实际插入且缺少 Embedding 的记忆创建任务。

### Step 10：准备候选审计数据

此时所有候选处理结果已经确定。

### Step 11：写 Turn Commit

`turn_commits` 必须是事务中最后一个领域锚点写入。

### Step 12：写候选审计

由于审计表外键依赖 `turn_commits`，在 Commit 记录之后写审计；二者仍在同一事务内。

### Step 13：提交

只有 `commit()` 已确认成功后才能更新 Context。

---

## 20. `PersistencePhase` 参考实现

```python
from __future__ import annotations

import asyncio

from cogito_agent.runtime.context import TurnContext
from cogito_agent.runtime.phase import BasePhase


class PersistencePhase(BasePhase):
    name = "persistence"

    def __init__(
        self,
        *,
        clock: ClockPort,
        uow_factory: UnitOfWorkFactoryPort,
        planner: PersistencePlanBuilder,
        sanitizer: PersistenceSanitizer,
        fingerprint: PersistenceFingerprint,
        preference_policy: PreferencePersistencePolicy,
        memory_policy: MemoryPersistencePolicy,
        retry_policy: PersistenceRetryPolicy,
        commit_recovery: CommitRecoveryService,
        embedding_port: EmbeddingPort | None,
        embedding_model: str,
    ) -> None:
        self._clock = clock
        self._uow_factory = uow_factory
        self._planner = planner
        self._sanitizer = sanitizer
        self._fingerprint = fingerprint
        self._preference_policy = preference_policy
        self._memory_policy = memory_policy
        self._retry_policy = retry_policy
        self._commit_recovery = commit_recovery
        self._embedding_port = embedding_port
        self._embedding_model = embedding_model

    async def execute(self, ctx: TurnContext) -> None:
        self._validate_context(ctx)

        sanitized = self._sanitizer.sanitize_context(ctx)
        plan = self._planner.build(
            ctx=ctx,
            sanitized=sanitized,
            now=self._clock.now(),
        )
        plan = await self._prepare_embeddings(plan)

        outcome = await self._execute_with_retry(plan)

        ctx.persistence_outcome = outcome
        ctx.persistence_completed = True
        ctx.metadata["persistence"] = {
            "commit_id": outcome.commit_id,
            "session_version": outcome.session_version,
            "summary_version": outcome.summary_version,
            "idempotent_replay": outcome.idempotent_replay,
            "embedding_job_count": outcome.embedding_job_count,
        }

    @staticmethod
    def _validate_context(ctx: TurnContext) -> None:
        if ctx.persistence_completed:
            raise PersistenceAlreadyCompletedError(
                "persistence has already completed"
            )

        if ctx.persistence_outcome is not None:
            raise InvalidPersistenceContextError(
                "persistence_outcome exists before persistence"
            )

        if not ctx.turn_id:
            raise InvalidPersistenceContextError("turn_id is required")

        if ctx.started_at is None:
            raise InvalidPersistenceContextError("started_at is required")

        if ctx.status is not TurnStatus.RUNNING:
            raise InvalidPersistenceContextError(
                f"invalid turn status: {ctx.status}"
            )

        if ctx.error is not None:
            raise InvalidPersistenceContextError(
                "cannot persist a turn containing an error"
            )

        if ctx.output_text is None:
            raise InvalidPersistenceContextError("output_text is required")

        request = ctx.request
        for field_name, value in (
            ("request_id", request.request_id),
            ("session_id", request.session_id),
            ("actor_id", request.actor_id),
        ):
            if not value.strip():
                raise InvalidPersistenceContextError(
                    f"{field_name} is required"
                )

    async def _prepare_embeddings(
        self,
        plan: PersistencePlan,
    ) -> PersistencePlan:
        if self._embedding_port is None:
            return plan

        candidates = plan.embedding_candidates()
        if not candidates:
            return plan

        try:
            vectors = await self._embedding_port.embed_many(
                tuple(candidate.content for candidate in candidates)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return plan.with_embedding_failure(
                model=self._embedding_model,
                safe_error_code="EMBEDDING_UNAVAILABLE",
            )

        if len(vectors) != len(candidates):
            raise EmbeddingProtocolError(
                "embedding result count does not match request count"
            )

        prepared = tuple(
            PreparedEmbedding.from_vector(
                candidate_id=candidate.candidate_id,
                vector=vector,
            )
            for candidate, vector in zip(candidates, vectors, strict=True)
        )
        return plan.with_embeddings(prepared)

    async def _execute_with_retry(
        self,
        plan: PersistencePlan,
    ) -> PersistenceOutcome:
        last_error: BaseException | None = None

        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                return await self._persist_once(plan)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                mapped = self._retry_policy.classify(exc)
                if not mapped.retryable:
                    raise mapped from exc

                last_error = mapped
                if attempt >= self._retry_policy.max_attempts:
                    raise mapped from exc

                await self._retry_policy.sleep_before_retry(attempt)

        raise PersistenceError("unreachable retry state") from last_error

    async def _persist_once(
        self,
        plan: PersistencePlan,
    ) -> PersistenceOutcome:
        async with self._uow_factory.create() as uow:
            existing = await uow.turn_commits.get_by_request(
                user_id=plan.user_id,
                request_id=plan.request_id,
            )
            if existing is not None:
                return self._resolve_replay(plan, existing)

            await uow.sessions.create_if_absent(
                session_id=plan.session_id,
                user_id=plan.user_id,
                now=plan.persistence_started_at,
            )
            session = await uow.sessions.get_for_write(
                session_id=plan.session_id,
            )
            if session is None:
                raise SessionNotFoundError(plan.session_id)

            self._validate_session(plan, session)

            persisted_events = self._assign_sequences(
                drafts=plan.events,
                base_seq_no=session.next_seq_no,
            )
            await uow.events.add_many(persisted_events)

            advanced_session = await uow.sessions.advance(
                session_id=plan.session_id,
                expected_version=session.version,
                consumed_sequences=len(persisted_events),
                last_turn_id=plan.turn_id,
                last_request_id=plan.request_id,
                last_message_at=persisted_events[-1].created_at,
            )

            summary_outcome, advanced_session = (
                await self._apply_summary(
                    uow=uow,
                    plan=plan,
                    session=advanced_session,
                )
            )

            preference_outcomes = await self._apply_preferences(
                uow=uow,
                plan=plan,
                persisted_events=persisted_events,
            )
            memory_outcomes = await self._apply_memories(
                uow=uow,
                plan=plan,
                persisted_events=persisted_events,
            )

            all_outcomes = (
                *((summary_outcome,) if summary_outcome else ()),
                *preference_outcomes,
                *memory_outcomes,
            )

            embedding_jobs = self._build_embedding_jobs(
                plan=plan,
                outcomes=memory_outcomes + preference_outcomes,
            )
            if embedding_jobs:
                await uow.embedding_jobs.add_many(embedding_jobs)

            outcome = self._build_outcome(
                plan=plan,
                session=advanced_session,
                events=persisted_events,
                candidate_outcomes=all_outcomes,
                embedding_job_count=len(embedding_jobs),
                idempotent_replay=False,
            )

            await uow.turn_commits.add(
                TurnCommitRecord.from_outcome(
                    plan=plan,
                    outcome=outcome,
                )
            )
            await uow.candidate_audits.add_many(
                CandidateWriteAudit.from_outcomes(
                    plan=plan,
                    outcomes=all_outcomes,
                )
            )

            await self._commit_with_known_outcome(
                uow=uow,
                plan=plan,
            )
            return outcome
```

---

## 21. 提交取消与结果恢复

### 21.1 提交前取消

取消发生在 `commit()` 调用前：

- `CancelledError` 原样传播；
- UoW `__aexit__` 回滚；
- `ctx.persistence_completed` 保持 `False`。

### 21.2 提交期间取消

提交一旦开始，必须先确定数据库结果：

```python
async def _commit_with_known_outcome(
    self,
    *,
    uow: UnitOfWorkPort,
    plan: PersistencePlan,
) -> None:
    commit_task = asyncio.create_task(uow.commit())

    try:
        await asyncio.shield(commit_task)
    except asyncio.CancelledError:
        try:
            await commit_task
        except Exception as exc:
            raise PersistenceCommitError(
                "commit failed while resolving cancellation"
            ) from exc
        raise
    except Exception as exc:
        recovered = await self._commit_recovery.lookup(
            user_id=plan.user_id,
            request_id=plan.request_id,
            expected_fingerprint=plan.commit_fingerprint,
        )
        if recovered is not None:
            return
        raise PersistenceCommitOutcomeUnknownError(
            "unable to determine SQLite commit outcome"
        ) from exc
```

语义说明：

- 调用方可能收到取消，但数据库已经提交；
- 后续重试使用相同 `request_id`，会得到等价幂等重放；
- 不允许因为调用方取消而在提交结果未知时再次盲写。

### 21.3 Commit Recovery

恢复服务使用独立只读连接查询：

```sql
SELECT commit_fingerprint, outcome_json
FROM turn_commits
WHERE user_id = :user_id
  AND request_id = :request_id;
```

规则：

- 找到且指纹一致：提交成功；
- 找到但指纹不一致：幂等冲突；
- 未找到：提交未发生或结果仍未知，进入有限重试；
- 多次仍无法确认：抛 `PersistenceCommitOutcomeUnknownError`。

---

## 22. 重试策略

### 22.1 可重试错误

```text
SQLITE_BUSY
SQLITE_LOCKED
临时 I/O 中断且确认未提交
Session 乐观版本冲突
Summary 版本冲突
```

### 22.2 不可重试错误

```text
Context 字段缺失
Session 所有权冲突
幂等指纹冲突
候选字段非法
JSON 序列化失败
外键或 CHECK 约束错误
重复且不可解释的业务唯一键冲突
数据库 schema 版本不匹配
```

### 22.3 推荐参数

```python
@dataclass(frozen=True, slots=True)
class PersistenceRetryConfig:
    max_attempts: int = 3
    delays_seconds: tuple[float, ...] = (0.05, 0.15)
```

SQLite 已设置 `busy_timeout=5000`，应用层重试只处理超时后仍失败的短暂冲突。

重试不得：

- 重新调用模型；
- 重新执行工具；
- 重新抽取知识；
- 重新生成随机计划内容；
- 修改 Commit Fingerprint。

---

## 23. 错误模型

```python
class PersistenceError(RuntimeAgentError):
    code = "PERSISTENCE_ERROR"
    retryable = False


class InvalidPersistenceContextError(PersistenceError):
    code = "PERSISTENCE_CONTEXT_INVALID"


class PersistenceAlreadyCompletedError(PersistenceError):
    code = "PERSISTENCE_ALREADY_COMPLETED"


class IdempotencyConflictError(PersistenceError):
    code = "PERSISTENCE_IDEMPOTENCY_CONFLICT"


class SessionOwnershipError(PersistenceError):
    code = "PERSISTENCE_SESSION_OWNERSHIP_ERROR"


class OptimisticConcurrencyError(PersistenceError):
    code = "PERSISTENCE_CONCURRENCY_ERROR"
    retryable = True


class SummaryConcurrencyError(PersistenceError):
    code = "PERSISTENCE_SUMMARY_CONCURRENCY_ERROR"
    retryable = True


class CandidateValidationError(PersistenceError):
    code = "PERSISTENCE_CANDIDATE_INVALID"


class EmbeddingProtocolError(PersistenceError):
    code = "PERSISTENCE_EMBEDDING_PROTOCOL_ERROR"


class PersistenceBusyError(PersistenceError):
    code = "PERSISTENCE_BUSY"
    retryable = True


class PersistenceCommitError(PersistenceError):
    code = "PERSISTENCE_COMMIT_ERROR"
    retryable = True


class PersistenceCommitOutcomeUnknownError(PersistenceError):
    code = "PERSISTENCE_COMMIT_OUTCOME_UNKNOWN"
    retryable = True
```

对 Channel 暴露的安全消息统一为：

```text
无法保存本轮 Agent 状态，请使用相同请求重试。
```

内部日志可以记录错误码、request_id、turn_id、session_id 和 SQLite 扩展错误码，但不得记录敏感内容。

---

## 24. Repository SQL 要点

### 24.1 批量写事件

```sql
INSERT INTO events (
    id,
    user_id,
    session_id,
    seq_no,
    role,
    event_type,
    content,
    content_json,
    request_id,
    turn_id,
    trace_id,
    created_by_span_id,
    extraction_status,
    created_at,
    updated_at
)
VALUES (
    :id,
    :user_id,
    :session_id,
    :seq_no,
    :role,
    :event_type,
    :content,
    :content_json,
    :request_id,
    :turn_id,
    :trace_id,
    :created_by_span_id,
    :extraction_status,
    :created_at,
    :created_at
);
```

禁止使用 `INSERT OR REPLACE`，因为它可能执行删除再插入，破坏外键、rowid 和审计语义。

### 24.2 查询 active memory

```sql
SELECT *
FROM memories
WHERE user_id = :user_id
  AND memory_key = :memory_key
  AND status = 'active'
LIMIT 1;
```

### 24.3 强化重复记忆

```sql
UPDATE memories
SET confidence = :confidence,
    importance = :importance,
    source_group_id = :source_group_id,
    source_event_ids_json = :source_event_ids_json,
    updated_by_span_id = :span_id
WHERE id = :memory_id
  AND status = 'active';
```

### 24.4 替代旧记忆

```sql
UPDATE memories
SET status = 'superseded',
    valid_until = COALESCE(valid_until, :now),
    updated_by_span_id = :span_id
WHERE id = :old_memory_id
  AND status = 'active';
```

随后插入新记录。两条语句必须处于同一事务。

### 24.5 写 Commit Record

```sql
INSERT INTO turn_commits (
    commit_id,
    user_id,
    session_id,
    request_id,
    turn_id,
    commit_fingerprint,
    user_event_id,
    assistant_event_id,
    session_version,
    outcome_json,
    persistence_span_id,
    committed_at
)
VALUES (
    :commit_id,
    :user_id,
    :session_id,
    :request_id,
    :turn_id,
    :commit_fingerprint,
    :user_event_id,
    :assistant_event_id,
    :session_version,
    :outcome_json,
    :persistence_span_id,
    :committed_at
);
```

---

## 25. Trace 与 AgentEvent 的边界

### 25.1 Kernel 负责生命周期事件

Kernel 继续统一发送：

```text
PHASE_STARTED(persistence)
PHASE_COMPLETED(persistence)
PHASE_FAILED(persistence)
PERSISTENCE_COMPLETED
```

`PersistencePhase` 不注入 `AgentEventSink`，避免重复事件和顺序不一致。

### 25.2 Trace 数据

- `trace_events` 的 Phase span 由 Trace Adapter 创建和结束；
- `ctx.current_span_id` 作为 `events.created_by_span_id`、`memories.created_by_span_id` 和 `updated_by_span_id`；
- 工具执行 span 由 `AgentLoopPhase` 的 Trace 组件记录；
- PersistencePhase 不重复创建工具 span；
- Trace 写入失败不得让领域事务引用不存在的 span，无法保证时应写 `NULL`。

### 25.3 持久化结果元数据

只向 Context 写摘要：

```json
{
  "commit_id": "...",
  "session_version": 12,
  "summary_version": 4,
  "idempotent_replay": false,
  "embedding_job_count": 1
}
```

不得写完整候选内容、工具输出或数据库行快照。

---

## 26. Bootstrap 组装

```python
persistence_phase = PersistencePhase(
    clock=system_clock,
    uow_factory=sqlite_uow_factory,
    planner=PersistencePlanBuilder(
        id_generator=stable_id_generator,
        fingerprint=PersistenceFingerprint(),
    ),
    sanitizer=PersistenceSanitizer(
        max_inline_tool_result_bytes=100_000,
    ),
    fingerprint=PersistenceFingerprint(),
    preference_policy=PreferencePersistencePolicy(),
    memory_policy=MemoryPersistencePolicy(),
    retry_policy=PersistenceRetryPolicy(
        config=PersistenceRetryConfig(
            max_attempts=3,
            delays_seconds=(0.05, 0.15),
        )
    ),
    commit_recovery=SQLiteCommitRecovery(
        connection_factory=sqlite_connection_factory,
    ),
    embedding_port=embedding_adapter,
    embedding_model="configured-model-name",
)
```

Pipeline 顺序保持：

```python
phases = [
    TurnInitPhase(...),
    StateLoadPhase(...),
    InformationRetrievalPhase(...),
    ContextAssemblyPhase(...),
    AgentLoopPhase(...),
    KnowledgeExtractionPhase(...),
    persistence_phase,
    TurnFinalizePhase(...),
]
```

Kernel 不需要为 Persistence 写任何名称分支。

---

## 27. 实施顺序

### 27.1 领域模型

先完成：

- `SessionSnapshot`；
- `ConversationEvent`；
- 最终候选模型；
- `PersistencePlan`；
- `PersistenceOutcome`；
- `CandidateWriteOutcome`。

### 27.2 Schema 迁移

完成：

- events/trace_events 新列；
- sessions；
- turn_commits；
- candidate_write_audits；
- embedding_jobs；
- schema version 检查。

### 27.3 Port 与 SQLite Adapter

完成 UoW 和各 Repository，先用集成测试验证原子性和约束。

### 27.4 纯领域组件

完成：

- Sanitizer；
- Plan Builder；
- Fingerprint；
- Preference Policy；
- Memory Policy；
- Retry Classifier。

这些组件优先写纯单元测试。

### 27.5 Phase 编排

实现 `PersistencePhase`，只负责编排，不直接写 SQL。

### 27.6 Bootstrap

在 Composition Root 注入所有依赖。

### 27.7 完整测试

通过单元、集成、并发、故障注入和架构边界测试后才能交付。

---

## 28. 单元测试清单

### 28.1 Context 校验

验证：

- 缺少 `turn_id`；
- 缺少 `started_at`；
- 缺少 `output_text`；
- status 非 RUNNING；
- `ctx.error` 非空；
- `persistence_completed=True`；
- 已存在 `persistence_outcome`；
- 空 request/session/actor ID。

所有情况不得打开数据库连接。

### 28.2 Plan Builder

验证：

- 用户事件第一；
- 助手事件最后；
- 工具事件按 ordinal 排序；
- 事件 ID 稳定；
- Candidate 来源正确映射到事件 ID；
- JSON 稳定序列化；
- 相同业务输入产生相同 Fingerprint；
- turn_id 或随机 ID 变化不改变 Fingerprint。

### 28.3 Sanitizer

验证：

- Authorization、API Key、Cookie 被移除；
- 大结果转外部引用；
- 不可序列化对象被拒绝；
- 长度限制生效；
- Unicode 规范化稳定。

### 28.4 Candidate Policy

覆盖每种操作：

```text
INSERT
UPDATE
DELETE
IGNORE
TENTATIVE
```

并覆盖：

- 新记录；
- 等价重复；
- 内容替代；
- 不存在时删除；
- 无效 Key；
- 无效置信度；
- 无效有效期。

### 28.5 Retry

验证：

- BUSY 重试；
- LOCKED 重试；
- 业务错误不重试；
- CancelledError 原样传播；
- 每次尝试使用新 UoW；
- 所有尝试复用同一 Plan。

---

## 29. SQLite 集成测试清单

### 29.1 原子提交

在写入中途注入异常，验证：

```text
没有新增 events
没有更新 sessions
没有新增/修改 memories
没有 turn_commits
没有 candidate_write_audits
```

### 29.2 正常提交

验证：

- 事件顺序正确；
- Session version 增加；
- next_seq_no 正确推进；
- 用户和助手事件唯一；
- Commit Outcome 可完整反序列化；
- FTS 能检索新 active memory。

### 29.3 幂等重放

相同 `request_id`、相同内容执行两次：

- 第二次 `idempotent_replay=True`；
- events 行数不增加；
- memories 不重复；
- Session version 不再次增加；
- 返回的事件 ID 与首次一致。

### 29.4 幂等冲突

相同 `request_id`、不同输出文本：

- 抛 `IdempotencyConflictError`；
- 原数据不变。

### 29.5 Session 并发

两个连接同时写同一 Session：

- 不出现重复 `seq_no`；
- 一个提交后另一个重试；
- 最终 version 单调递增；
- 不覆盖 Summary。

### 29.6 记忆替代

验证：

- 旧 active 变为 superseded；
- 新记录 active；
- `supersedes_id` 正确；
- 任意时刻最多一个 active key；
- FTS 查询只返回 active 结果。

### 29.7 Tentative

验证：

- 不写 active memories；
- candidate audit 为 tentative。

### 29.8 Embedding 失败

验证：

- 核心 Turn 仍提交；
- memory.embedding 为 NULL；
- embedding_jobs 有 pending 任务；
- 后续补偿写入 BLOB 后任务变 done。

### 29.9 完整性

每组测试结束执行：

```sql
PRAGMA quick_check;
PRAGMA foreign_key_check;
```

结果必须为空错误。

---

## 30. 故障注入测试

必须注入以下故障点：

```text
BEGIN IMMEDIATE 失败
写第一个 event 后失败
写工具事件后失败
推进 Session 后失败
更新 Summary 后失败
替代旧 memory 后、新 memory 插入前失败
写 turn_commits 后、commit 前失败
commit 调用抛异常但数据库实际已提交
commit 期间任务取消
EventSink 抛异常
```

验证目标：

- 不出现半提交；
- 不出现两个 active memory；
- 不出现无 Commit 锚点的完整 Turn；
- Commit 结果未知时不会盲目重复写；
- EventSink 失败不改变领域事务结果。

---

## 31. 架构边界测试

自动扫描导入关系，确保：

```text
runtime/phases/persistence.py
    不导入 sqlite3
    不导入 aiosqlite
    不导入 SQLAlchemy
    不导入 MessageBus
    不导入 Channel SDK

runtime/persistence/*
    不导入 infrastructure.*

ports/*
    不导入具体数据库类型

infrastructure/sqlite/*
    可以实现 ports
    不反向被 domain 导入
```

还应检查：

- Repository 不存在 `commit()`；
- Phase 不存在 SQL 字符串；
- Kernel 不存在 `if phase.name == "persistence"`；
- Phase 不直接调用 EventSink；
- 业务状态不依赖 `metadata` 中的隐藏字段。

---

## 32. 性能与锁控制

### 32.1 写事务必须短

事务内只允许：

- SQLite 查询；
- SQLite 写入；
- 纯内存候选状态判断；
- JSON 序列化的最终小对象。

事务外完成：

- LLM；
- 工具；
- 文件下载；
- 大文件写入；
- Embedding；
- 复杂文本清洗；
- Commit Fingerprint 计算。

### 32.2 批量操作

- 事件使用 `executemany`；
- 审计使用 `executemany`；
- 候选先排序，再逐条执行需要读取当前状态的操作；
- 不为每条事件重新打开连接。

### 32.3 不使用 `INSERT OR REPLACE`

原因：

- 可能触发隐式删除；
- 破坏外键；
- 改变 rowid；
- 破坏 FTS 外部内容表一致性；
- 掩盖幂等冲突。

使用明确的：

```text
INSERT
UPDATE
INSERT ... ON CONFLICT DO NOTHING
```

---

## 33. 安全与隐私

持久化前必须执行：

- 工具参数字段白名单；
- 密钥字段遮盖；
- 错误消息安全化；
- 大结果外置；
- 用户删除策略标记；
- Embedding 输入与存储策略检查。

禁止写入：

- API Key；
- OAuth Access Token；
- Refresh Token；
- Cookie；
- Authorization Header；
- 数据库 DSN；
- 完整隐藏 Prompt；
- 模型私有推理链；
- 未经允许的高敏感个人数据。

候选审计只保存：

- 候选 ID；
- Key；
- 操作；
- 结果状态；
- 原因码；
- 来源事件 ID；
- 置信度与重要性。

不重复保存完整敏感内容。

---

## 34. 运维要求

定期执行：

```sql
PRAGMA quick_check;
PRAGMA foreign_key_check;
```

维护窗口执行：

```sql
PRAGMA integrity_check;
INSERT INTO memories_fts(memories_fts) VALUES ('optimize');
```

FTS 不一致时：

```sql
INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');
```

受控备份使用 SQLite Backup API。运行中不得只复制主 `.db` 文件而忽略 `-wal` 和 `-shm`。

---

## 35. 验收标准

实现只有同时满足以下条件才算完成：

### 35.1 正确性

- 一次 Turn 的领域写入原子提交；
- 相同请求重放不重复写入；
- 幂等键内容冲突被拒绝；
- Session 消息序号严格递增；
- Session 和 Summary 版本不会静默覆盖；
- 同一用户同一 memory_key 最多一个 active 记录；
- Tentative 和 Ignore 不进入 active memory；
- Commit 未确认时不设置 `persistence_completed`。

### 35.2 架构

- Phase 只依赖 Port 和纯领域组件；
- SQL 全部位于 Infrastructure；
- Repository 不自行提交；
- Kernel 不感知数据库；
- Phase 不感知 MessageBus 和 Channel；
- TurnContext 的核心持久化结果强类型化。

### 35.3 可靠性

- BUSY/LOCKED 有限重试；
- Commit 结果未知可恢复；
- 取消不会产生无保护的重复写；
- Embedding 失败不会丢失核心消息和记忆；
- 所有候选都有审计结果；
- 数据库完整性检查通过。

### 35.4 测试

- 单元测试覆盖所有分支；
- SQLite 集成测试覆盖真实事务；
- 并发测试覆盖同 Session 写入；
- 故障注入覆盖所有关键写入点；
- 架构测试阻止错误依赖方向。

---

## 36. 最终执行路径摘要

```text
PersistencePhase.execute(ctx)
    │
    ├─ validate_context
    ├─ sanitize_context
    ├─ build_immutable_plan
    ├─ prepare_embeddings_outside_transaction
    │
    └─ retry_loop
         │
         └─ BEGIN IMMEDIATE
              ├─ find turn_commit by (user_id, request_id)
              │    ├─ same fingerprint → return replay outcome
              │    └─ different fingerprint → fail
              │
              ├─ create/load session
              ├─ validate ownership and version
              ├─ allocate seq_no
              ├─ insert ordered events
              ├─ advance session
              ├─ update summary
              ├─ apply preferences
              ├─ apply memories
              ├─ create embedding jobs when needed
              ├─ insert turn_commit
              ├─ insert candidate audits
              └─ COMMIT

COMMIT confirmed
    ├─ ctx.persistence_outcome = outcome
    ├─ ctx.persistence_completed = True
    └─ TurnFinalizePhase may execute
```

这一路径保证 `PersistencePhase` 同时具备明确边界、SQLite 事务正确性、可重复执行能力、候选审计能力、并发控制能力和故障恢复能力，并与 Cogito-Agent 的 Channel 无关、MessageBus 无关、固定 Phase Pipeline 架构保持一致。
