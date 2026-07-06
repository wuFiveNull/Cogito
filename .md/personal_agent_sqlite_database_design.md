# 个人 Agent 的 SQLite 数据库设计方案

> 版本：1.0  
> 适用场景：单机、桌面端、移动端、边缘设备或低写入并发的个人 Agent  
> 核心能力：对话批量提取记忆、混合检索、工具调用记录、全链路追踪

---

## 1. 结论

SQLite 可以完成这套个人 Agent 的数据存储任务，适合以下场景：

- 每个用户或每台设备维护一个本地数据库文件；
- Agent 主要运行在单机或同一设备上；
- 读请求较多，写请求并发较低；
- 希望数据本地化、部署简单、便于备份和迁移；
- 记忆规模尚未大到必须使用独立向量数据库或分布式数据库。

推荐保留三张核心业务表：

```text
trace_events
    记录 Agent 的模型调用、记忆检索、工具调用和决策步骤

events
    保存用户消息、Agent 回复、工具请求和工具结果

memories
    保存从一段对话中提取出的长期记忆
```

另外创建一个 FTS5 虚拟表：

```text
memories_fts
    为 memories 提供中文关键词和子串检索
```

`memories_fts` 是搜索索引，不是新的业务事实源。权威数据仍然在 `memories`。

---

## 2. SQLite 的适用边界

### 2.1 适合

- 单用户个人 Agent；
- 一个应用进程或少量本机进程；
- 本地优先应用；
- 每秒写入次数不高；
- 需要事务、外键、JSON 查询、全文检索和本地向量检索；
- 希望整个数据系统可以复制为一个数据库文件。

### 2.2 不适合

出现以下情况时，应考虑迁移到 PostgreSQL 或服务化数据库：

- 多台服务器同时写同一个数据库；
- 大量并发写入；
- 需要跨机器共享同一个 WAL 数据库；
- 多租户数据量和查询压力持续上升；
- 需要复杂的行级权限、在线分析或分布式高可用；
- 工具调用、消息和追踪日志达到非常高的持续写入吞吐。

SQLite 在 WAL 模式下允许读写并行，但同一时刻仍只有一个写入者。因此，它适合个人 Agent，但不适合作为高并发多租户 Agent 平台的长期核心数据库。

---

## 3. 版本要求

推荐：

```text
SQLite >= 3.51.3
```

原因：

- 使用 `STRICT` 表；
- 使用 `RETURNING`；
- 使用 JSON 函数；
- 使用 FTS5 trigram tokenizer；
- 使用 WAL 模式；
- SQLite 官方在 3.51.3 修复了一个涉及 WAL、多连接写入和 checkpoint 的竞态问题。

如果运行环境无法升级到 3.51.3 或更高版本：

- 可以暂时使用回滚日志模式；
- 或确保只有单连接写入并谨慎管理 checkpoint；
- 不建议在未知版本上直接启用多进程 WAL 写入。

应用启动时应检查版本：

```sql
SELECT sqlite_version();
```

---

## 4. 连接初始化配置

每次创建数据库连接后执行：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

可选配置：

```sql
PRAGMA wal_autocheckpoint = 1000;
PRAGMA journal_size_limit = 67108864;
```

说明：

| 配置 | 作用 |
|---|---|
| `foreign_keys=ON` | 启用外键约束 |
| `journal_mode=WAL` | 提高读写并行性 |
| `synchronous=NORMAL` | 在性能和持久性之间取平衡 |
| `busy_timeout=5000` | 遇到写锁时等待最多 5 秒 |
| `wal_autocheckpoint=1000` | WAL 达到约 1000 页时自动 checkpoint |
| `journal_size_limit` | 限制 WAL 或 journal 文件长期占用空间 |

注意：

- WAL 数据库的 `.db`、`-wal` 和 `-shm` 文件在运行时是一个整体；
- 应通过 SQLite Backup API 或受控 checkpoint 后复制数据库；
- 不要在写入过程中只复制主 `.db` 文件。

---

## 5. ID 和时间规范

### 5.1 ID

SQLite 没有内置 UUID 类型，所有业务 ID 使用 `TEXT`：

```text
trace_id
span_id
event_id
memory_id
session_id
tool_call_id
extraction_group_id
```

建议由应用层生成 UUIDv7 或其他单调递增的唯一 ID。

示例：

```text
0198f9b3-04bf-7d1c-b827-c5fbfced47cc
```

UUIDv7 的时间有序性有利于索引局部性，但不是强制要求。

### 5.2 时间

所有时间使用 UTC ISO 8601 文本：

```text
2026-06-24T09:30:15.123Z
```

数据库默认值：

```sql
strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
```

用户时区只在展示层转换，不在数据库中混合存储本地时间。

---

## 6. 总体数据模型

```text
trace_events
    id ───────────────┐
                      ├── events.created_by_span_id
                      ├── memories.created_by_span_id
                      └── memories.updated_by_span_id

events
    extraction_group_id
                      └── memories.source_group_id

events.id
                      └── memories.source_event_ids_json[]

memories.id
                      └── memories.supersedes_id
```

核心原则：

1. `events` 保存原始事实；
2. `memories` 保存经过提取和整理的长期状态；
3. `trace_events` 保存系统做过的动作和动作之间的因果关系；
4. 工具输入和输出写入 `events`；
5. 工具调用过程写入 `trace_events`；
6. 所有跨表的多值引用在 MVP 中使用 JSON 数组；
7. 数据规模或关联查询复杂后，再拆分关系表。

---

# 7. 完整建表 SQL

以下 SQL 已按 SQLite 语法设计。

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE trace_events (
    id                      TEXT PRIMARY KEY,
    trace_id                TEXT NOT NULL,
    parent_span_id          TEXT REFERENCES trace_events(id),

    user_id                 TEXT NOT NULL,
    session_id              TEXT,

    step_type               TEXT NOT NULL,
    step_name               TEXT NOT NULL,

    status                  TEXT NOT NULL DEFAULT 'running'
                            CHECK (
                                status IN (
                                    'running',
                                    'success',
                                    'failed',
                                    'timeout',
                                    'cancelled'
                                )
                            ),

    input_event_ids_json    TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(input_event_ids_json)
                                AND json_type(input_event_ids_json) = 'array'
                            ),

    input_memory_ids_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(input_memory_ids_json)
                                AND json_type(input_memory_ids_json) = 'array'
                            ),

    output_event_ids_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(output_event_ids_json)
                                AND json_type(output_event_ids_json) = 'array'
                            ),

    output_memory_ids_json  TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(output_memory_ids_json)
                                AND json_type(output_memory_ids_json) = 'array'
                            ),

    model_name              TEXT,
    prompt_version          TEXT,

    tool_name               TEXT,
    tool_call_id            TEXT,
    attempt_no              INTEGER NOT NULL DEFAULT 1
                            CHECK (attempt_no >= 1),

    decision                TEXT,
    decision_reason         TEXT,

    metadata_json           TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(metadata_json)),

    input_hash              TEXT,
    output_hash             TEXT,

    started_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    ended_at                TEXT,
    latency_ms              INTEGER CHECK (
                                latency_ms IS NULL OR latency_ms >= 0
                            ),

    error_code              TEXT,
    error_message           TEXT,

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;

CREATE TABLE events (
    id                      TEXT PRIMARY KEY,

    user_id                 TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    seq_no                  INTEGER NOT NULL CHECK (seq_no >= 1),

    role                    TEXT NOT NULL
                            CHECK (
                                role IN (
                                    'user',
                                    'assistant',
                                    'system',
                                    'tool'
                                )
                            ),

    event_type              TEXT NOT NULL,

    content                 TEXT NOT NULL DEFAULT '',
    content_json            TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(content_json)),

    trace_id                TEXT,
    created_by_span_id      TEXT REFERENCES trace_events(id),

    extraction_status       TEXT NOT NULL DEFAULT 'pending'
                            CHECK (
                                extraction_status IN (
                                    'pending',
                                    'processing',
                                    'done',
                                    'failed',
                                    'ignored'
                                )
                            ),

    extraction_group_id     TEXT,
    extraction_attempts     INTEGER NOT NULL DEFAULT 0
                            CHECK (extraction_attempts >= 0),
    extraction_error        TEXT,
    extracted_at            TEXT,

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),

    UNIQUE(user_id, session_id, seq_no)
) STRICT;

CREATE TABLE memories (
    id                      TEXT PRIMARY KEY,

    user_id                 TEXT NOT NULL,

    memory_type             TEXT NOT NULL
                            CHECK (
                                memory_type IN (
                                    'fact',
                                    'preference',
                                    'rule',
                                    'event'
                                )
                            ),

    memory_key              TEXT NOT NULL,

    content                 TEXT NOT NULL,
    value_json              TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(value_json)),

    embedding               BLOB,
    embedding_dim           INTEGER CHECK (
                                embedding_dim IS NULL OR embedding_dim > 0
                            ),
    embedding_model         TEXT,
    embedding_format        TEXT DEFAULT 'float32-le',

    importance              REAL NOT NULL DEFAULT 0.5
                            CHECK (
                                importance >= 0.0
                                AND importance <= 1.0
                            ),

    confidence              REAL NOT NULL DEFAULT 0.8
                            CHECK (
                                confidence >= 0.0
                                AND confidence <= 1.0
                            ),

    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (
                                status IN (
                                    'active',
                                    'superseded',
                                    'deleted'
                                )
                            ),

    valid_from              TEXT,
    valid_until             TEXT,

    source_group_id         TEXT,

    source_event_ids_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(source_event_ids_json)
                                AND json_type(source_event_ids_json) = 'array'
                            ),

    supersedes_id           TEXT REFERENCES memories(id),

    created_by_span_id      TEXT REFERENCES trace_events(id),
    updated_by_span_id      TEXT REFERENCES trace_events(id),

    access_count            INTEGER NOT NULL DEFAULT 0
                            CHECK (access_count >= 0),
    last_accessed_at        TEXT,

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;

CREATE INDEX idx_trace_events_trace
ON trace_events(trace_id, started_at);

CREATE INDEX idx_trace_events_parent
ON trace_events(parent_span_id);

CREATE INDEX idx_trace_events_user_session
ON trace_events(user_id, session_id, started_at DESC);

CREATE INDEX idx_trace_events_step_type
ON trace_events(step_type, started_at DESC);

CREATE INDEX idx_trace_events_tool
ON trace_events(tool_name, started_at DESC)
WHERE tool_name IS NOT NULL;

CREATE INDEX idx_trace_events_tool_call
ON trace_events(tool_call_id, attempt_no)
WHERE tool_call_id IS NOT NULL;

CREATE INDEX idx_events_session_seq
ON events(user_id, session_id, seq_no);

CREATE INDEX idx_events_trace
ON events(trace_id, created_at);

CREATE INDEX idx_events_pending_extraction
ON events(user_id, session_id, extraction_status, seq_no)
WHERE extraction_status IN ('pending', 'failed');

CREATE INDEX idx_events_extraction_group
ON events(extraction_group_id, seq_no)
WHERE extraction_group_id IS NOT NULL;

CREATE INDEX idx_events_type_time
ON events(user_id, event_type, created_at DESC);

CREATE UNIQUE INDEX idx_memories_active_key
ON memories(user_id, memory_key)
WHERE status = 'active';

CREATE INDEX idx_memories_user_status
ON memories(user_id, status);

CREATE INDEX idx_memories_user_type
ON memories(user_id, memory_type, status);

CREATE INDEX idx_memories_validity
ON memories(user_id, valid_from, valid_until)
WHERE status = 'active';

CREATE INDEX idx_memories_source_group
ON memories(source_group_id)
WHERE source_group_id IS NOT NULL;

CREATE INDEX idx_memories_created_span
ON memories(created_by_span_id);

CREATE TRIGGER trg_events_touch_updated_at
AFTER UPDATE ON events
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE events
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE id = NEW.id;
END;

CREATE TRIGGER trg_memories_touch_updated_at
AFTER UPDATE ON memories
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE memories
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE id = NEW.id;
END;

CREATE VIRTUAL TABLE memories_fts USING fts5(
    user_id UNINDEXED,
    memory_key,
    content,
    content = 'memories',
    content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER memories_fts_ai
AFTER INSERT ON memories
BEGIN
    INSERT INTO memories_fts(
        rowid,
        user_id,
        memory_key,
        content
    )
    VALUES (
        NEW.rowid,
        NEW.user_id,
        NEW.memory_key,
        NEW.content
    );
END;

CREATE TRIGGER memories_fts_ad
AFTER DELETE ON memories
BEGIN
    INSERT INTO memories_fts(
        memories_fts,
        rowid,
        user_id,
        memory_key,
        content
    )
    VALUES (
        'delete',
        OLD.rowid,
        OLD.user_id,
        OLD.memory_key,
        OLD.content
    );
END;

CREATE TRIGGER memories_fts_au
AFTER UPDATE OF user_id, memory_key, content ON memories
BEGIN
    INSERT INTO memories_fts(
        memories_fts,
        rowid,
        user_id,
        memory_key,
        content
    )
    VALUES (
        'delete',
        OLD.rowid,
        OLD.user_id,
        OLD.memory_key,
        OLD.content
    );

    INSERT INTO memories_fts(
        rowid,
        user_id,
        memory_key,
        content
    )
    VALUES (
        NEW.rowid,
        NEW.user_id,
        NEW.memory_key,
        NEW.content
    );
END;
```

执行完成后，可以设置 schema 版本：

```sql
PRAGMA user_version = 1;
```

后续迁移时逐步增加：

```text
1 → 2 → 3
```

不要依赖运行时自动猜测当前表结构。

---

# 8. `trace_events` 设计

## 8.1 作用

`trace_events` 记录 Agent 的每一个执行步骤，例如：

```text
接收用户消息
保存消息
解析意图
检索记忆
调用模型
决定调用工具
调用日历工具
调用搜索工具
写入记忆
生成最终回答
```

一次完整用户请求共享同一个 `trace_id`。

每个步骤使用独立 `id`，该 `id` 就是 `span_id`。

## 8.2 父子链路

```text
Trace T100
└── S1 agent_request
    ├── S2 receive_message
    ├── S3 retrieve_memories
    ├── S4 call_calendar_tool
    ├── S5 call_restaurant_tool
    └── S6 generate_response
```

对应：

```text
S1.parent_span_id = NULL
S2.parent_span_id = S1
S3.parent_span_id = S1
S4.parent_span_id = S1
S5.parent_span_id = S1
S6.parent_span_id = S1
```

## 8.3 推荐的 `step_type`

```text
input
event_write
segment
model_call
memory_extract
memory_write
memory_update
memory_retrieve
tool_call
decision
response
error
```

`step_type` 不使用数据库枚举，便于未来增加类型。

## 8.4 Span 写入模式

开始时插入：

```sql
INSERT INTO trace_events (
    id,
    trace_id,
    parent_span_id,
    user_id,
    session_id,
    step_type,
    step_name,
    status,
    started_at
)
VALUES (
    :span_id,
    :trace_id,
    :parent_span_id,
    :user_id,
    :session_id,
    :step_type,
    :step_name,
    'running',
    :started_at
);
```

完成时更新：

```sql
UPDATE trace_events
SET status = 'success',
    output_event_ids_json = :output_event_ids_json,
    output_memory_ids_json = :output_memory_ids_json,
    metadata_json = :metadata_json,
    ended_at = :ended_at,
    latency_ms = :latency_ms,
    output_hash = :output_hash
WHERE id = :span_id
  AND status = 'running';
```

失败时：

```sql
UPDATE trace_events
SET status = 'failed',
    ended_at = :ended_at,
    latency_ms = :latency_ms,
    error_code = :error_code,
    error_message = :safe_error_message
WHERE id = :span_id
  AND status = 'running';
```

## 8.5 不保存完整思维链

只保存简短、结构化、可审计的原因：

```json
{
  "decision": "call_tool",
  "decision_reason": "用户询问实时天气，已有记忆不包含当前天气",
  "metadata": {
    "trigger": "requires_realtime_information",
    "selected_tool": "weather_lookup"
  }
}
```

不要保存：

- 模型隐藏思维过程；
- 未脱敏的系统 Prompt；
- API Key；
- Access Token；
- Cookie；
- 完整支付信息；
- 无必要的高敏感个人信息。

---

# 9. `events` 设计

## 9.1 作用

`events` 是原始事件日志，保存：

```text
user_message
assistant_message
system_message
tool_request
tool_result
tool_error
email
calendar_event
document
external_data
```

## 9.2 用户消息示例

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
    trace_id,
    created_by_span_id
)
VALUES (
    :event_id,
    :user_id,
    :session_id,
    :seq_no,
    'user',
    'user_message',
    '我最近搬到杭州滨江了',
    '{}',
    :trace_id,
    :span_id
);
```

## 9.3 Agent 回复示例

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
    trace_id,
    created_by_span_id,
    extraction_status
)
VALUES (
    :event_id,
    :user_id,
    :session_id,
    :seq_no,
    'assistant',
    'assistant_message',
    :answer,
    :answer_metadata_json,
    :trace_id,
    :response_span_id,
    'pending'
);
```

Agent 回复也保留为 `pending`，因为批量提取模型可能需要它理解指代和上下文。

但是提取 Prompt 必须明确：

> 助手消息只能帮助理解上下文，不能单独证明用户事实。

---

# 10. 对话批量提取

## 10.1 不使用单独批次表

对话提取批次直接由 `events` 中的两个字段表示：

```text
extraction_status
extraction_group_id
```

状态流转：

```text
pending → processing → done
                    ↘ failed
```

没有提取出任何记忆时也设置为 `done`，避免重复处理。

## 10.2 推荐触发条件

满足任意一项：

- 用户超过 10 分钟没有继续发消息；
- 累积达到 4～6 个用户回合；
- 对话达到约 2500 tokens；
- 当前任务明确结束；
- 用户说“记住这些”；
- 达到 5000 tokens 硬上限。

## 10.3 SQLite 中的并发领取方式

SQLite 不支持 PostgreSQL 的 `FOR UPDATE SKIP LOCKED`。

应使用：

```sql
BEGIN IMMEDIATE;
```

`BEGIN IMMEDIATE` 会提前获取写锁，确保同一时刻只有一个 Worker 分配提取组。

假设应用已经确定本次片段范围为 `start_seq` 到 `end_seq`：

```sql
BEGIN IMMEDIATE;

UPDATE events
SET extraction_status = 'processing',
    extraction_group_id = :group_id,
    extraction_attempts = extraction_attempts + 1,
    extraction_error = NULL
WHERE user_id = :user_id
  AND session_id = :session_id
  AND extraction_status = 'pending'
  AND seq_no BETWEEN :start_seq AND :end_seq;

SELECT changes() AS claimed_event_count;

COMMIT;
```

应用必须检查：

```text
claimed_event_count > 0
```

如果是 0，说明这些消息已被其他 Worker 领取或状态已经改变。

## 10.4 读取对话片段

```sql
SELECT
    id,
    seq_no,
    role,
    event_type,
    content,
    content_json,
    created_at
FROM events
WHERE extraction_group_id = :group_id
ORDER BY seq_no;
```

## 10.5 重叠上下文

如果当前批次中出现：

```text
就按刚才那个预算。
```

可以额外读取上一批最后 2～4 条消息：

```sql
SELECT
    id,
    seq_no,
    role,
    event_type,
    content
FROM events
WHERE user_id = :user_id
  AND session_id = :session_id
  AND seq_no < :start_seq
ORDER BY seq_no DESC
LIMIT 4;
```

发送给模型时区分：

```text
<context_only>
这些消息只用于理解，不要从中重复提取。
</context_only>

<target_events>
这些消息是本次需要提取的范围。
</target_events>
```

---

# 11. 记忆提取输出

模型只输出候选 JSON，不直接写数据库。

```json
{
  "memories": [
    {
      "memory_type": "fact",
      "memory_key": "residence.district",
      "content": "用户目前居住在杭州滨江区",
      "value_json": {
        "city": "杭州",
        "district": "滨江区"
      },
      "importance": 0.8,
      "confidence": 0.95,
      "valid_from": null,
      "valid_until": null,
      "source_event_ids": [
        "event-1",
        "event-3"
      ]
    },
    {
      "memory_type": "preference",
      "memory_key": "restaurant.preferred_area",
      "content": "为用户推荐餐厅时优先选择杭州滨江区附近",
      "value_json": {
        "preferred_area": "杭州滨江区"
      },
      "importance": 0.7,
      "confidence": 0.95,
      "source_event_ids": [
        "event-4"
      ]
    }
  ]
}
```

提取规则：

```text
允许提取：
- 用户明确表达的长期事实
- 相对稳定的偏好
- 长期限制
- 对 Agent 的行为要求
- 对未来决策有价值的重要事件

禁止提取：
- 普通寒暄
- 一次性任务参数
- 助手提出但用户没有确认的信息
- 模型自己的猜测
- 已被用户否定的信息
- 与用户无关的通用知识
```

---

# 12. `memories` 设计

## 12.1 记忆类型

| 类型 | 含义 | 示例 |
|---|---|---|
| `fact` | 当前事实 | 用户目前住在杭州 |
| `preference` | 稳定偏好 | 用户喜欢安静的餐厅 |
| `rule` | Agent 行为规则 | 付费前必须征得用户确认 |
| `event` | 有未来价值的历史事件 | 上次推荐餐厅太吵，用户不满意 |

## 12.2 `memory_key`

用于去重和更新：

```text
residence.city
residence.district
work.location
food.allergy.peanut
restaurant.ambience
restaurant.preferred_area
email.writing_style
booking.payment_confirmation
```

多值属性把值写入 Key：

```text
food.allergy.peanut
food.allergy.shellfish
food.dislike.cilantro
```

历史事件使用唯一 Key：

```text
event.restaurant_feedback.2026-06-01.a82f
event.trip.2026-05.tokyo
```

同一用户和同一个 `memory_key` 只允许一条 `active` 记忆，由部分唯一索引保证。

## 12.3 新记忆

```sql
INSERT INTO memories (
    id,
    user_id,
    memory_type,
    memory_key,
    content,
    value_json,
    embedding,
    embedding_dim,
    embedding_model,
    importance,
    confidence,
    valid_from,
    valid_until,
    source_group_id,
    source_event_ids_json,
    created_by_span_id,
    updated_by_span_id
)
VALUES (
    :memory_id,
    :user_id,
    :memory_type,
    :memory_key,
    :content,
    :value_json,
    :embedding,
    :embedding_dim,
    :embedding_model,
    :importance,
    :confidence,
    :valid_from,
    :valid_until,
    :source_group_id,
    :source_event_ids_json,
    :span_id,
    :span_id
);
```

## 12.4 重复记忆

新旧内容含义基本相同：

```sql
UPDATE memories
SET confidence = MIN(1.0, confidence + 0.05),
    source_group_id = :source_group_id,
    source_event_ids_json = :source_event_ids_json,
    updated_by_span_id = :span_id
WHERE id = :memory_id
  AND status = 'active';
```

第一版会覆盖旧来源数组。

需要保留全部来源时，可以在应用层合并 JSON 数组，或者后续增加 `memory_sources` 关系表。

## 12.5 替代旧记忆

旧：

```text
用户住在上海
```

新：

```text
用户已搬到杭州
```

使用单个事务：

```sql
BEGIN IMMEDIATE;

UPDATE memories
SET status = 'superseded',
    valid_until = COALESCE(:new_valid_from, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_by_span_id = :span_id
WHERE id = :old_memory_id
  AND status = 'active';

INSERT INTO memories (
    id,
    user_id,
    memory_type,
    memory_key,
    content,
    value_json,
    embedding,
    embedding_dim,
    embedding_model,
    importance,
    confidence,
    valid_from,
    source_group_id,
    source_event_ids_json,
    supersedes_id,
    created_by_span_id,
    updated_by_span_id
)
VALUES (
    :new_memory_id,
    :user_id,
    'fact',
    'residence.city',
    '用户目前居住在杭州',
    '{"city":"杭州"}',
    :embedding,
    :embedding_dim,
    :embedding_model,
    0.8,
    0.95,
    :new_valid_from,
    :source_group_id,
    :source_event_ids_json,
    :old_memory_id,
    :span_id,
    :span_id
);

COMMIT;
```

---

# 13. 提取完成和失败

## 13.1 成功

记忆写入和事件状态更新放在同一事务中：

```sql
BEGIN IMMEDIATE;

-- 插入或更新 memories

UPDATE events
SET extraction_status = 'done',
    extraction_error = NULL,
    extracted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE extraction_group_id = :group_id
  AND extraction_status = 'processing';

COMMIT;
```

## 13.2 失败

```sql
UPDATE events
SET extraction_status = 'failed',
    extraction_error = :safe_error_message
WHERE extraction_group_id = :group_id
  AND extraction_status = 'processing';
```

重试：

```sql
BEGIN IMMEDIATE;

UPDATE events
SET extraction_status = 'processing',
    extraction_attempts = extraction_attempts + 1,
    extraction_error = NULL
WHERE extraction_group_id = :group_id
  AND extraction_status = 'failed'
  AND extraction_attempts < 3;

SELECT changes() AS retried_event_count;

COMMIT;
```

超过限制后保留为 `failed`，供人工查看。

---

# 14. 全文检索

## 14.1 为什么使用 FTS5 trigram

个人 Agent 的内容通常包含中文、英文、人名和项目名。

方案使用：

```sql
tokenize = 'trigram'
```

trigram 会把连续三个字符作为 token，可以支持更通用的子串匹配。

限制：

- 少于 3 个 Unicode 字符的查询不能直接通过 trigram 全文查询匹配；
- 对于“张总”“杭州”等短词，应回退到 `LIKE`、精确 Key 查询或应用层关键词扩展。

## 14.2 FTS 查询

```sql
SELECT
    m.id,
    m.memory_type,
    m.memory_key,
    m.content,
    m.value_json,
    m.importance,
    m.confidence,
    bm25(memories_fts) AS lexical_rank
FROM memories_fts
JOIN memories m
  ON m.rowid = memories_fts.rowid
WHERE memories_fts MATCH :fts_query
  AND m.user_id = :user_id
  AND m.status = 'active'
  AND (
      m.valid_from IS NULL
      OR m.valid_from <= :query_time
  )
  AND (
      m.valid_until IS NULL
      OR m.valid_until > :query_time
  )
ORDER BY lexical_rank
LIMIT 30;
```

注意：

```text
bm25 值越小，排名越靠前。
```

不建议直接把 BM25 数值与向量相似度相加。更稳妥的是使用排名融合。

## 14.3 短关键词回退

```sql
SELECT
    id,
    memory_type,
    memory_key,
    content,
    value_json,
    importance,
    confidence
FROM memories
WHERE user_id = :user_id
  AND status = 'active'
  AND (
      content LIKE '%' || :keyword || '%'
      OR memory_key = :memory_key
  )
LIMIT 20;
```

因为个人 Agent 数据量通常不大，短词回退的线性扫描可以先接受；后续再针对热点字段增加结构化索引。

---

# 15. 向量存储和检索

SQLite 核心本身没有原生向量类型，因此提供两种方案。

## 15.1 推荐 MVP：BLOB + 应用层精确计算

`memories.embedding` 保存 Float32 数组的二进制形式：

```text
embedding_format = float32-le
embedding_dim = 模型维度
embedding_model = 模型名称和版本
```

写入原则：

1. 先对向量做 L2 归一化；
2. 以 little-endian Float32 编码；
3. 存储到 BLOB；
4. 查询时读取同一用户的 active embedding；
5. 在应用层计算点积或余弦相似度。

查询候选：

```sql
SELECT
    id,
    memory_type,
    memory_key,
    content,
    importance,
    confidence,
    embedding,
    embedding_dim
FROM memories
WHERE user_id = :user_id
  AND status = 'active'
  AND embedding IS NOT NULL
  AND (
      valid_from IS NULL
      OR valid_from <= :query_time
  )
  AND (
      valid_until IS NULL
      OR valid_until > :query_time
  );
```

适合：

- 本地单用户；
- 记忆规模有限；
- 希望减少外部扩展依赖；
- 需要先验证产品效果。

优化方式：

- 先使用 `memory_type`、时间、Key 和 FTS 缩小候选；
- 再对候选 embedding 做精确重排；
- 在应用内缓存 active embedding；
- 记忆更新后增量刷新缓存。

## 15.2 可选：使用 `sqlite-vec`

需要数据库内 KNN 时，可加载第三方向量扩展 `sqlite-vec`。

示例：

```sql
CREATE VIRTUAL TABLE memory_vectors USING vec0(
    embedding float[1536]
);
```

写入时使用 `memories.rowid` 作为向量表 rowid：

```sql
INSERT INTO memory_vectors(rowid, embedding)
SELECT rowid, :embedding
FROM memories
WHERE id = :memory_id;
```

查询：

```sql
SELECT
    m.id,
    m.memory_type,
    m.memory_key,
    m.content,
    v.distance
FROM memory_vectors v
JOIN memories m
  ON m.rowid = v.rowid
WHERE v.embedding MATCH :query_embedding
  AND m.user_id = :user_id
  AND m.status = 'active'
ORDER BY v.distance
LIMIT 30;
```

注意：

- `sqlite-vec` 目前属于第三方扩展；
- 其项目明确标注为 pre-v1，可能存在破坏性升级；
- 生产环境应固定扩展版本；
- 应保留 `memories.embedding` 或重建能力，以便升级后重新生成向量索引；
- 不要把虚拟向量表当作唯一事实源。

---

# 16. 混合检索流程

推荐流程：

```text
用户问题
  ↓
查询分析
  ↓
结构化 Key / 类型过滤
  ↓
FTS5 关键词召回
  ↓
向量召回或候选向量重排
  ↓
合并去重
  ↓
时间、重要性和置信度排序
  ↓
选择 5～10 条
  ↓
传给主模型
```

## 16.1 查询分析结果

```json
{
  "memory_types": [
    "fact",
    "preference",
    "rule",
    "event"
  ],
  "keywords": [
    "张总",
    "餐厅",
    "生食"
  ],
  "semantic_queries": [
    "用户选择餐厅时的偏好和限制",
    "与张总有关的历史用餐反馈"
  ]
}
```

## 16.2 推荐使用 RRF 融合

不要直接混合 BM25 和向量距离的绝对值。

使用 Reciprocal Rank Fusion：

```text
RRF_score =
    Σ 1 / (k + rank_channel)
```

例如：

```text
k = 60
```

可叠加业务分：

```text
final_score =
    0.70 × normalized_rrf
  + 0.15 × importance
  + 0.10 × confidence
  + 0.05 × exact_key_bonus
```

第一版也可以使用更直观的规则：

```text
精确 Key 命中优先
多通道命中优先
过期记忆排除
active 状态优先
高重要性约束优先
最后按综合分取 5～10 条
```

---

# 17. 记录记忆检索链路

检索完成后写入一个 `memory_retrieve` span：

```json
{
  "id": "span-retrieve-1",
  "trace_id": "trace-100",
  "parent_span_id": "span-root-1",
  "user_id": "user-1",
  "session_id": "session-1",
  "step_type": "memory_retrieve",
  "step_name": "retrieve_personal_memories",
  "status": "success",
  "input_event_ids_json": [
    "current-user-message-id"
  ],
  "output_memory_ids_json": [
    "memory-1",
    "memory-2",
    "memory-3"
  ],
  "decision": "select_top_memories",
  "decision_reason": "根据精确 Key、FTS、向量相关性、时间和重要性选择",
  "metadata_json": {
    "candidate_memory_ids": [
      "memory-1",
      "memory-2",
      "memory-3",
      "memory-4"
    ],
    "selected_memory_ids": [
      "memory-1",
      "memory-2",
      "memory-3"
    ],
    "channels": {
      "memory-1": ["key", "fts", "vector"],
      "memory-2": ["vector"],
      "memory-3": ["fts", "vector"]
    },
    "scores": {
      "memory-1": 0.91,
      "memory-2": 0.86,
      "memory-3": 0.79
    }
  }
}
```

区分：

```text
candidate_memory_ids
    所有候选

selected_memory_ids
    真正传入主模型的记忆
```

---

# 18. 工具调用记录

工具调用使用：

```text
trace_events
    记录工具调用过程、状态、重试和耗时

events
    保存工具请求参数、执行结果或错误
```

## 18.1 工具请求 Event

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
    trace_id,
    created_by_span_id,
    extraction_status
)
VALUES (
    :request_event_id,
    :user_id,
    :session_id,
    :seq_no,
    'tool',
    'tool_request',
    '调用餐厅搜索工具',
    :request_json,
    :trace_id,
    :tool_span_id,
    'pending'
);
```

`request_json`：

```json
{
  "tool_name": "restaurant_search",
  "tool_version": "v2",
  "arguments": {
    "city": "杭州",
    "district": "滨江",
    "ambience": "安静",
    "exclude_foods": ["生食"]
  }
}
```

## 18.2 工具结果 Event

```json
{
  "tool_name": "restaurant_search",
  "result_count": 8,
  "results": [
    {
      "name": "餐厅 A",
      "district": "滨江"
    }
  ]
}
```

## 18.3 工具错误 Event

```json
{
  "tool_name": "restaurant_search",
  "error_code": "TIMEOUT",
  "message": "调用超过 5 秒"
}
```

## 18.4 工具调用 Span

```json
{
  "trace_id": "trace-100",
  "parent_span_id": "span-root-1",
  "step_type": "tool_call",
  "step_name": "search_restaurants",
  "status": "success",
  "tool_name": "restaurant_search",
  "tool_call_id": "tool-call-100",
  "attempt_no": 1,
  "input_event_ids_json": [
    "tool-request-event"
  ],
  "output_event_ids_json": [
    "tool-result-event"
  ],
  "decision": "call_tool",
  "decision_reason": "用户需要当前餐厅信息，长期记忆无法提供实时结果",
  "metadata_json": {
    "tool_version": "v2",
    "result_count": 8
  },
  "latency_ms": 460
}
```

## 18.5 重试

同一个逻辑调用使用相同 `tool_call_id`，不同尝试使用不同 span：

```text
tool_call_id = TC100

attempt 1：timeout
attempt 2：failed
attempt 3：success
```

查询：

```sql
SELECT
    id,
    tool_name,
    tool_call_id,
    attempt_no,
    status,
    latency_ms,
    error_code,
    started_at
FROM trace_events
WHERE tool_call_id = :tool_call_id
ORDER BY attempt_no;
```

---

# 19. 大型工具结果

不要把几 MB 的网页、文件或搜索结果直接塞入 `content_json`。

建议：

```text
小于 100 KB：
    直接保存到 events.content_json

大于 100 KB：
    保存到本地对象文件或对象存储
    events.content_json 只保存引用、摘要、大小和 Hash
```

示例：

```json
{
  "tool_name": "document_reader",
  "storage_ref": "file:///agent-data/tool-results/result-123.json",
  "result_hash": "sha256:abc123...",
  "result_size": 4200000,
  "summary": "读取了 128 页文档"
}
```

如果应用完全本地运行，`storage_ref` 可以指向应用数据目录中的文件。

数据库备份时应同时备份这些外部文件。

---

# 20. 最终回答记录

Agent 回复写入 `events`。

最终生成回答的 span：

```json
{
  "trace_id": "trace-100",
  "step_type": "response",
  "step_name": "generate_final_response",
  "status": "success",
  "input_memory_ids_json": [
    "memory-1",
    "memory-2",
    "memory-3"
  ],
  "input_event_ids_json": [
    "tool-result-event"
  ],
  "output_event_ids_json": [
    "assistant-message-event"
  ],
  "model_name": "main-agent-model",
  "prompt_version": "agent-response-v1",
  "metadata_json": {
    "provided_memory_ids": [
      "memory-1",
      "memory-2",
      "memory-3"
    ],
    "used_memory_ids": [
      "memory-1",
      "memory-2"
    ],
    "provided_tool_result_ids": [
      "tool-result-event"
    ],
    "used_tool_result_ids": [
      "tool-result-event"
    ]
  }
}
```

`used_memory_ids` 和 `used_tool_result_ids` 通常由模型自报，只能作为辅助证据。

更可靠的数据是：

- 哪些记忆被传入模型；
- 哪些工具结果被传入模型；
- 最终 Prompt 版本；
- 输入和输出 Hash；
- 最终回答 Event。

---

# 21. 完整链路示例

用户：

```text
看一下我下周什么时候有空，然后找一家适合和张总吃饭的餐厅。
```

在线 Trace：

```text
Trace T100
│
├── S1 receive_user_message
│   └── 输出 Event E1
│
├── S2 retrieve_memories
│   └── 输出 Memory M1、M2
│
├── S3 call_calendar_tool
│   ├── Event E2：tool_request
│   └── Event E3：tool_result
│
├── S4 select_available_time
│
├── S5 call_restaurant_search
│   ├── Event E4：tool_request
│   └── Event E5：tool_result
│
└── S6 generate_response
    ├── 使用 Memory M1、M2
    ├── 使用 Event E3、E5
    └── 输出 Event E6：assistant_message
```

十分钟后触发后台提取：

```text
Trace T200
│
├── S20 create_extraction_group
│   └── 输入 Event E1～E6
│
├── S21 extract_memory_candidates
│
├── S22 write_memory
│   └── 输出 Memory M10
│
└── S23 finish_extraction
```

后台提取使用新的 `trace_id`，但通过以下字段仍可以关联回原始请求：

```text
input_event_ids_json
source_event_ids_json
source_group_id
```

---

# 22. 常用查询

## 22.1 查询完整 Trace

```sql
SELECT *
FROM trace_events
WHERE trace_id = :trace_id
ORDER BY started_at, created_at;
```

## 22.2 查询 Trace 中所有工具调用

```sql
SELECT
    id,
    parent_span_id,
    tool_name,
    tool_call_id,
    attempt_no,
    status,
    latency_ms,
    error_code,
    decision_reason,
    started_at,
    ended_at
FROM trace_events
WHERE trace_id = :trace_id
  AND step_type = 'tool_call'
ORDER BY started_at;
```

## 22.3 查询一条记忆的来源消息

```sql
SELECT
    m.id AS memory_id,
    m.content AS memory_content,
    e.id AS event_id,
    e.role,
    e.event_type,
    e.content AS event_content,
    e.created_at
FROM memories m
JOIN json_each(m.source_event_ids_json) source
JOIN events e
  ON e.id = source.value
WHERE m.id = :memory_id
ORDER BY e.seq_no;
```

## 22.4 查询最终回答使用的输入

```sql
SELECT
    id,
    input_event_ids_json,
    input_memory_ids_json,
    output_event_ids_json,
    model_name,
    prompt_version,
    metadata_json
FROM trace_events
WHERE trace_id = :trace_id
  AND step_type = 'response'
ORDER BY started_at DESC
LIMIT 1;
```

## 22.5 查询一条记忆的替代链

从当前记忆向旧版本追溯：

```sql
WITH RECURSIVE memory_history AS (
    SELECT
        id,
        supersedes_id,
        memory_key,
        content,
        status,
        valid_from,
        valid_until,
        created_at
    FROM memories
    WHERE id = :current_memory_id

    UNION ALL

    SELECT
        old.id,
        old.supersedes_id,
        old.memory_key,
        old.content,
        old.status,
        old.valid_from,
        old.valid_until,
        old.created_at
    FROM memories old
    JOIN memory_history current
      ON current.supersedes_id = old.id
)
SELECT *
FROM memory_history
ORDER BY created_at DESC;
```

## 22.6 查询待提取会话

```sql
SELECT
    user_id,
    session_id,
    COUNT(*) AS pending_event_count,
    MIN(seq_no) AS first_pending_seq,
    MAX(seq_no) AS last_pending_seq,
    MIN(created_at) AS first_pending_at,
    MAX(created_at) AS last_pending_at
FROM events
WHERE extraction_status = 'pending'
GROUP BY user_id, session_id
ORDER BY last_pending_at;
```

## 22.7 查询失败提取组

```sql
SELECT
    extraction_group_id,
    user_id,
    session_id,
    COUNT(*) AS event_count,
    MAX(extraction_attempts) AS attempts,
    MAX(extraction_error) AS last_error
FROM events
WHERE extraction_status = 'failed'
GROUP BY extraction_group_id, user_id, session_id
ORDER BY MAX(updated_at) DESC;
```

---

# 23. 事务边界

以下操作必须使用事务。

## 23.1 分配提取组

```text
锁定写入
→ 更新 pending events
→ 检查领取数量
→ 提交
```

## 23.2 写入新记忆并完成批次

```text
插入或更新 memories
→ 更新 trace span
→ events 设置 done
→ 提交
```

## 23.3 替代旧记忆

```text
旧记忆设置 superseded
→ 插入新记忆
→ 写 memory_update trace
→ 提交
```

## 23.4 工具调用完成

```text
写 tool_result event
→ 更新 tool_call span
→ 提交
```

事务应尽量短。

不要在数据库写事务中等待：

- LLM 请求；
- 网络工具请求；
- 文件下载；
- embedding API。

正确顺序：

```text
读取必要数据
→ 提交或结束读事务
→ 调用外部服务
→ 开启短写事务
→ 保存结果
→ 提交
```

这样可以避免长时间占用 SQLite 唯一写锁。

---

# 24. 幂等性

所有后台任务和工具结果写入必须幂等。

推荐幂等 Key：

| 操作 | 幂等 Key |
|---|---|
| 保存消息 | `event.id` |
| 创建提取组 | `extraction_group_id` |
| 模型调用 | `span.id` |
| 工具逻辑调用 | `tool_call_id` |
| 工具单次尝试 | `tool_call_id + attempt_no` |
| 当前记忆 | `user_id + memory_key + active` |
| 外部事件导入 | `source_system + external_id`，放入 `content_json` |

工具请求重放前先查：

```sql
SELECT *
FROM trace_events
WHERE tool_call_id = :tool_call_id
  AND attempt_no = :attempt_no;
```

如果已经 `success`，不要再次调用外部工具。

---

# 25. 完整性检查与维护

## 25.1 数据库完整性

定期执行：

```sql
PRAGMA quick_check;
```

维护窗口执行：

```sql
PRAGMA integrity_check;
```

## 25.2 外键检查

```sql
PRAGMA foreign_key_check;
```

## 25.3 FTS 重建

如果 FTS 索引与主表不一致：

```sql
INSERT INTO memories_fts(memories_fts)
VALUES ('rebuild');
```

## 25.4 FTS 优化

```sql
INSERT INTO memories_fts(memories_fts)
VALUES ('optimize');
```

不需要每次写入后执行，可在数据批量变化后或维护时执行。

## 25.5 WAL checkpoint

正常情况下 SQLite 会自动 checkpoint。

受控备份或维护时：

```sql
PRAGMA wal_checkpoint(TRUNCATE);
```

不要在高峰写入时频繁执行 `TRUNCATE` checkpoint。

---

# 26. 备份与恢复

推荐使用 SQLite Backup API，而不是直接复制运行中的主数据库文件。

备份范围：

```text
agent.db
外部工具结果目录
embedding 模型和版本信息
应用配置中的 Prompt 版本
```

Python 示例：

```python
import sqlite3

source = sqlite3.connect("agent.db")
target = sqlite3.connect("backup/agent-backup.db")

with target:
    source.backup(target)

target.close()
source.close()
```

恢复后执行：

```sql
PRAGMA quick_check;
PRAGMA foreign_key_check;
```

如果向量索引是可重建派生数据，可以在恢复后重建。

---

# 27. 隐私与保留策略

建议：

| 数据 | 建议保留 |
|---|---|
| `memories` | 长期，直到用户修改或删除 |
| 用户和 Agent 对话事件 | 根据产品隐私策略 |
| 详细 `trace_events` | 30～90 天 |
| 大型工具原始结果 | 7～30 天 |
| 错误堆栈 | 短期，且必须脱敏 |
| Hash 和关键审计摘要 | 可以长期保留 |

逻辑删除记忆：

```sql
UPDATE memories
SET status = 'deleted',
    updated_by_span_id = :span_id
WHERE id = :memory_id
  AND status != 'deleted';
```

检索必须始终过滤：

```sql
status = 'active'
```

用户要求彻底删除时，需要同时清理：

- `memories` 行；
- embedding；
- 可重建向量索引；
- 缓存；
- 外部对象文件；
- 备份中的对应数据，按隐私策略执行；
- 可能包含该信息的原始事件。

---

# 28. 推荐默认参数

```text
SQLite 版本：3.51.3+
数据库模式：WAL
busy_timeout：5000 ms

对话空闲触发：10 分钟
正常提取批次：4～6 个用户回合
Token 软上限：2500
Token 硬上限：5000
重叠上下文：上一批最后 2～4 条消息
每批最大记忆数：5
提取失败重试：2 次

FTS 召回：30 条
向量召回：30 条
最终传给模型：5～10 条
详细 Trace 保留：90 天
大型工具结果内联阈值：100 KB
```

---

# 29. 应用服务划分

```text
EventService
    保存用户消息、Agent 回复和工具事件

TraceService
    创建 trace/span
    完成 span
    记录错误、模型、工具和耗时

ConversationSegmenter
    判断一段对话是否结束
    分配 extraction_group_id

MemoryExtractionWorker
    读取对话片段
    调用模型提取候选记忆

MemoryWriter
    校验候选
    按 memory_key 去重
    插入、更新或替代记忆
    生成 embedding

MemoryRetriever
    Key 精确查询
    FTS5 检索
    向量检索
    融合排序

ToolRunner
    写 tool_request event
    调用工具
    写 tool_result/tool_error event
    更新 tool_call span
```

---

# 30. 迁移到 PostgreSQL 的条件

符合以下任一条件时开始评估迁移：

- 经常出现写锁等待；
- 需要多个服务实例同时高频写入；
- 单个数据库服务多个活跃用户；
- 需要数据库级行权限；
- 需要服务端在线备份、高可用和自动故障切换；
- 追踪和工具事件写入吞吐显著增长；
- 向量检索需要成熟的 ANN 索引和复杂过滤；
- 需要跨机器共享数据库。

迁移路径相对直接：

| SQLite | PostgreSQL |
|---|---|
| `TEXT UUID` | `UUID` |
| JSON `TEXT` | `JSONB` |
| 时间 `TEXT` | `TIMESTAMPTZ` |
| embedding `BLOB` | `pgvector VECTOR(n)` |
| FTS5 | PostgreSQL FTS 或 OpenSearch |
| `BEGIN IMMEDIATE` | 行锁 / `FOR UPDATE SKIP LOCKED` |

业务主表和字段语义不需要大改。

---

# 31. 最终建议

MVP 使用：

```text
SQLite
├── trace_events
├── events
├── memories
└── memories_fts
```

向量检索第一阶段使用：

```text
embedding BLOB
+ 应用层精确相似度
```

后续性能不足时增加：

```text
sqlite-vec
```

但 `memories` 始终是权威数据源，FTS 和向量表都只是可重建索引。

这套设计可以完整回答：

```text
Agent 收到了什么输入？
Agent 执行了哪些步骤？
为什么调用某个工具？
工具输入和输出是什么？
工具失败和重试了几次？
回答使用了哪些记忆？
记忆来自哪段历史对话？
为什么旧记忆被替代？
哪一步失败或耗时过长？
```

---

# 32. 参考资料

- SQLite JSON Functions: https://sqlite.org/json1.html
- SQLite FTS5: https://sqlite.org/fts5.html
- SQLite WAL: https://sqlite.org/wal.html
- SQLite PRAGMA: https://sqlite.org/pragma.html
- sqlite-vec: https://github.com/asg017/sqlite-vec
