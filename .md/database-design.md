# 类 Akashic Agent 数据库设计方案

## 一、设计原则

1. **按关注面分库** — 每个数据库负责一个独立领域，避免单库表爆炸和锁竞争
2. **source_ref 贯穿全链路** — 每条记录都能追溯到源头消息
3. **事件驱动 + 异步写入** — 主链路不阻塞，可观测数据通过事件总线异步写入
4. **无 ORM，手写 SQL + DAO** — 每个 Store 自包含 Schema 和连接管理
5. **内联迁移** — `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`，无迁移文件依赖

---

## 二、数据库拆分设计（共 5 库）

| 数据库 | 责任 | 核心表数 | 写入方式 |
|--------|------|----------|----------|
| `sessions.db` | 会话元数据 + 消息历史 | 2+1(FTS) | 同步，RLock |
| `memory.db` | 语义记忆存储 + 向量搜索 | 4 | 同步，RLock |
| `trace.db` | 全链路观测遥测 | 3 | 异步，asyncio.Queue |
| `graph.db` | 图联想记忆引擎（可选） | 8 | 同步，RLock |
| `state.db` | Agent 运行时状态 / 主动行为 | 9 | 同步+WAL，RLock |

---

## 三、各库详细设计

### 1. `sessions.db` — 会话与消息历史

**用途**：存储所有对话会话和消息，是整个系统的数据源头。

#### 表：`sessions`

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| `key` | TEXT | PK | 格式 `"channel:chat_id"`，如 `"cli:default"` |
| `created_at` | TEXT | NOT NULL | ISO8601 UTC |
| `updated_at` | TEXT | NOT NULL | ISO8601 UTC |
| `last_consolidated` | INTEGER | DEFAULT 0 | 上次合并的消息序号 |
| `metadata` | TEXT | | JSON，会话级设置 |
| `last_user_at` | TEXT | | 上次用户发言时间（后加字段） |
| `last_proactive_at` | TEXT | | 上次主动推送时间（后加字段） |
| `next_seq` | INTEGER | DEFAULT 0 | 下一条消息的序号（后加字段） |

#### 表：`messages`

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| `id` | TEXT | PK | 格式 `"session_key:seq"` → 全局唯一，构成 `source_ref` 基础 |
| `session_key` | TEXT | NOT NULL | → `sessions.key` |
| `seq` | INTEGER | NOT NULL | 会话内自增序号 |
| `role` | TEXT | NOT NULL | `user` / `assistant` / `system` / `tool` |
| `content` | TEXT | | 消息文本 |
| `tool_chain` | TEXT | | JSON，工具调用链完整记录 |
| `extra` | TEXT | | JSON，扩展元信息 |
| `ts` | TEXT | NOT NULL | ISO8601 |
| UNIQUE | (`session_key`, `seq`) | | |

#### 虚拟表：`messages_fts`

- FTS5，trigram 分词器（支持 CJK）
- 三个触发器自动同步 `INSERT / UPDATE / DELETE`

**作用**：`messages.id` 是 `source_ref` 的**根**——所有下游实体（记忆条目、遥测事件）都通过 `source_ref` 指向这里的消息 ID。

---

### 2. `memory.db` — 语义记忆向量库

**用途**：Agent 的长期语义记忆，支持向量语义检索 + 关键词搜索 + 幂等写入 + 替换审计。

#### 表：`memory_items` — 记忆主表

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| `id` | TEXT | PK | MD5[:12] 内容派生 ID |
| `memory_type` | TEXT | NOT NULL | `procedure` / `preference` / `event` / `profile` |
| `summary` | TEXT | NOT NULL | 供 LLM 阅读的记忆摘要 |
| `content_hash` | TEXT | NOT NULL | SHA256[:16] → **幂等键**：同内容同类型不新建 |
| `embedding` | TEXT | | JSON 浮点数数组（1024 维） |
| `reinforcement` | INTEGER | DEFAULT 1 | 重复遇见到递增，影响检索排序 |
| `emotional_weight` | INTEGER | DEFAULT 0 | 0-10 情感权重 |
| `extra_json` | TEXT | | `scope_channel`, `scope_chat_id`, `trigger_tags` 等 |
| `source_ref` | TEXT | | **可追溯** → `sessions.db` 中的消息 ID 或 JSON 数组 |
| `happened_at` | TEXT | | 事件发生时间 |
| `status` | TEXT | DEFAULT 'active' | `active` / `superseded` |
| `created_at` | TEXT | NOT NULL | |
| `updated_at` | TEXT | NOT NULL | |

索引：
```sql
UNIQUE(content_hash, memory_type)  -- 幂等写入保证
INDEX(status)                       -- 过滤活跃条目
```

#### 表：`vec_items` — 向量索引（sqlite-vec 虚拟表）

```sql
-- 1024 维 KNN 向量搜索
CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[1024]);
-- rowid 与 memory_items.rowid 共享
```

**回退方案**：当 sqlite-vec 扩展不可用时，全表扫描 + 余弦计算。

#### 表：`consolidation_events` — 合并去重索引

| 列 | 类型 | 说明 |
|----|------|------|
| `source_ref` | TEXT PK | 唯一来源引用，确保每条消息来源只合并一次 |
| `item_id` | TEXT | → `memory_items.id` |
| `created_at` | TEXT | |

#### 表：`memory_replacements` — 替换审计日志

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK AUTO | |
| `old_item_id` | TEXT NOT NULL | 被替换的旧记忆 |
| `old_memory_type / old_summary / old_source_ref / old_happened_at / old_extra_json` | TEXT | 旧快照 |
| `new_item_id` | TEXT NOT NULL | 新记忆 |
| `new_memory_type / new_summary / new_source_ref / new_happened_at / new_extra_json` | TEXT | 新快照 |
| `relation_type` | TEXT DEFAULT 'supersede' | 替换类型 |
| `source_ref` | TEXT | 触发替换的来源 |
| `created_at` | TEXT | |

索引：
```sql
INDEX(old_item_id, created_at)
INDEX(new_item_id, created_at)
```

---

### 3. `trace.db` — 全链路观测遥测

**用途**：记录 Agent 每次运行的遥测数据，用于监控、调试、分析。

设计模式：**异步写入** — `asyncio.Queue(maxsize=500)` + 后台 consumer，不阻塞主循环。Queue 满时 drop + 计数，永不崩溃主流程。

#### 表：`turns` — Agent 循环单步遥测

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK AUTO | |
| `ts` | TEXT NOT NULL | ISO8601 |
| `source` | TEXT NOT NULL | `'agent'` / `'proactive'` |
| `session_key` | TEXT NOT NULL | → `sessions.key` |
| `user_msg` | TEXT | 用户消息原文 |
| `llm_output` | TEXT NOT NULL | LLM 最终输出 |
| `raw_llm_output` | TEXT | 清洗前的原始输出 |
| `meme_tag / meme_media_count` | TEXT/INT | 命中的模板标签 |
| `tool_calls` | TEXT | JSON：每次工具调用 `{name, args, result}` |
| `tool_chain_json` | TEXT | JSON：完整 ReAct 迭代链 |
| `history_window / history_messages / history_chars / history_tokens` | INT | 上下文窗口统计 |
| `prompt_tokens` | INT | |
| `next_turn_baseline_tokens` | INT | |
| `react_iteration_count / react_input_sum_tokens / react_input_peak_tokens / react_final_input_tokens` | INT | 多步推理 token 消耗 |
| `react_cache_prompt_tokens / react_cache_hit_tokens` | INT | 缓存命中统计 |
| `error` | TEXT | NULL=正常 |

索引：
```sql
INDEX(session_key, ts)
INDEX(source, ts)
```

#### 表：`rag_queries` — 记忆检索完整记录

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK AUTO | |
| `ts` | TEXT NOT NULL | |
| `caller` | TEXT NOT NULL | `'passive'` / `'proactive'` / `'explicit'` |
| `session_key` | TEXT NOT NULL | |
| `query` | TEXT NOT NULL | 改写后的检索查询 |
| `orig_query` | TEXT | 改写前原文 |
| `aux_queries` | TEXT | JSON，HyDE 假设条目 |
| `hits_json` | TEXT | JSON：`[{id, type, score, summary, injected}]` |
| `injected_count` | INT NOT NULL DEFAULT 0 | 实际注入上下文的条数 |
| `route_decision` | TEXT | `'RETRIEVE'` / `'NO_RETRIEVE'` |
| `error` | TEXT | |

索引：
```sql
INDEX(session_key, ts)
INDEX(caller, ts)
```

#### 表：`memory_writes` — 记忆写入审计

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK AUTO | |
| `ts` | TEXT NOT NULL | |
| `session_key` | TEXT NOT NULL | |
| `source_ref` | TEXT | **来源引用** |
| `action` | TEXT NOT NULL | `'write'` / `'supersede'` |
| `memory_type` | TEXT | 写入的记忆类型 |
| `item_id` | TEXT | `'new:xxx'` / `'reinforced:xxx'` |
| `summary` | TEXT | |
| `superseded_ids` | TEXT | JSON，被替换的旧 ID |
| `error` | TEXT | |

索引：
```sql
INDEX(session_key, ts)
INDEX(action, ts)
```

**保留策略**（示例）：
- `turns`：180 天，错误记录永久保留
- `rag_queries`：90 天，错误记录永久保留
- `memory_writes`：90 天

---

### 4. `graph.db` — 图联想记忆引擎（可选）

**用途**：将消息组织为有向图，通过图扩散实现联想式记忆召回。适用于需要跨话题联想的高级场景。

#### 表：`memory_nodes` — 图节点

每条消息或记忆为图中的一个节点。

| 列 | 类型 | 说明 |
|----|------|------|
| `key` | TEXT PK | 格式 `"session_key:turn_seq"` |
| `anchor_id` | TEXT NOT NULL | 原始消息 ID |
| `session_key / turn_seq` | TEXT/INT | 来源位置 |
| `salience` | REAL | 显著性（0-1）|
| `strength` | REAL | 强度 0-3.0，随时间衰减 |
| `resource` | REAL | 疲劳度 0-1 |
| `recall_count` | INTEGER | 被召回次数 |
| `last_activated_ts` | REAL | Unix 时间戳 |
| `embedding` | BLOB | float32 序列化向量 |
| `emb_count` | INTEGER | 聚合的消息数 |

#### 表：`memory_edges` — 共激活边

| 列 | 类型 | 说明 |
|----|------|------|
| `(src_key, dst_key)` | TEXT, TEXT | PK |
| `weight` | REAL | Hebbian 学习权重 |
| `co_count` | INTEGER | 共激活次数 |
| `last_used_ts` | REAL | |

#### 辅助表

- `activation_events` — 每次查询的节点激活诊断
- `query_log` — 检索查询日志
- `embedding_cache` — 消息级 embedding 缓存 `(message_id, model) → BLOB`
- `salience_state` — 全局 embedding 质心（单行）
- `migration_runs / source_session_snapshot` — 数据迁移审计

---

### 5. `state.db` — Agent 运行时状态

**用途**：Agent 主动行为（自主运行）、去重、冷却、KV 存储。

#### 核心表

| 表 | 作用 | 核心字段 |
|----|------|----------|
| `seen_items` | 去重：已看过的外部内容 | `(source_key, item_id)` PK |
| `deliveries` | 去重：已推送过的消息 | `(session_key, delivery_key)` PK |
| `rejection_cooldown` | 冷却：用户拒绝过的内容 | `(source_key, item_id)` PK |
| `semantic_items` | 语义兴趣匹配文本 | `source_key, item_id, text` |
| `kv_state` | 全局键值状态 | `(key, value)` |
| `session_state` | 会话级键值状态 | `(session_key, key, value)` |
| `tick_log` | 主动 tick 执行日志 | 门控、步数、告警、消息 |
| `tick_step_log` | tick 内每一步诊断 | 工具调用、参数、结果 |

此库应使用 **WAL 模式**（高并发写入）和 `synchronous=NORMAL`。

---

## 四、全链路可追溯设计

### 核心机制：`source_ref`

`source_ref` 是贯穿整个系统的追溯 ID。构造规则：

| 来源 | 格式 | 示例 |
|------|------|------|
| 消息 ID | `session_key:seq` | `"cli:default:42"` |
| Consolidation 窗口 | `JSON([msg_ids])` | `'["cli:default:42","cli:default:43"]'` |
| 合并子条目 | `base_source_ref#h:{sha1[:12]}` | `'["cli:default:42"]#h:a1b2c3d4e5f6'` |
| 后处理 | `session_key@post_response` | `"cli:default@post_response"` |
| 隐式提取 | `base_source_ref#{type}` | `"cli:default:42#profile"` / `"cli:default:42#procedure"` |

### 全链路追溯流

```
用户输入 "上次推荐的那个餐厅叫什么？"
  │
  ├→ sessions.db.messages: 写入用户消息 (id="cli:default:42")
  │
  ├→ [Agent 处理流程]
  │   ├→ [记忆检索]
  │   │   ├→ trace.db.rag_queries: 记录 query/改写/命中项/source_ref
  │   │   └→ memory.db.memory_items: 命中已有记忆（带 source_ref 回溯）
  │   │
  │   ├→ [工具执行]
  │   │   └→ trace.db.turns: 记录 tool_calls + tool_chain_json
  │   │
  │   └→ [LLM 输出]
  │       └→ trace.db.turns: 记录 llm_output + token 统计
  │
  ├→ [应答写入 sessions.db.messages: assistant 回复 (id="cli:default:43")
  │
  ├→ [TurnCommitted 事件] → 异步处理:
  │   ├→ trace.db.turns: 写入完整 turn 记录
  │   ├→ sessions.db.sessions: 更新 last_consolidated 等
  │   └→ [Consolidation 处理]:
  │       ├→ memory.db.memory_items: 写入提取的记忆条目（source_ref 指向消息 ID）
  │       ├→ memory.db.consolidation_events: 记录去重索引
  │       └→ memory.db.memory_replacements: 审计替换操作
  │
  └→ [后处理]
      ├→ memory.db.memory_items.status='superseded': 使旧条目失效
      ├→ memory.db.memory_replacements: 记录替换审计
      └→ trace.db.memory_writes: 记录写入操作
```

### 三方向追溯能力

1. **正向追溯**：给定一条消息 ID → 查询 `memory_items.source_ref` → 找到该消息生成的所有记忆
2. **反向追溯**：给定一个记忆条目 → 解析 `source_ref` → `sessions.db.fetch_by_ids()` → 获取原始消息上下文
3. **变更审计**：给定一个记忆替换操作 → `memory_replacements` 记录新旧快照 + 完整 source_ref

```python
# 正向：消息 → 生成的记忆
SELECT * FROM memory_items WHERE source_ref LIKE '%cli:default:42%';

# 反向：记忆 → 消息原文
SELECT * FROM messages WHERE id IN (parse_source_ref(memory_item.source_ref));

# 变更历史：记忆被替换了哪些版本
SELECT * FROM memory_replacements 
WHERE old_item_id = 'xxx' OR new_item_id = 'xxx'
ORDER BY created_at;
```

---

## 五、连接管理 + 迁移策略

### 连接模式（每个 Store 通用）

```python
class MyStore:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.RLock()  # 可重入锁
        self._closed = False
        self._init_schema()
        self._migrate()
```

| 方面 | 方案 |
|------|------|
| 线程安全 | `check_same_thread=False` + `threading.RLock()` |
| 行工厂 | `sqlite3.Row`（按列名访问） |
| WAL 模式 | 仅高频写入的 `state.db` 使用，其余默认 |
| 连接关闭 | `close()` 幂等，`__del__()` 兜底 |
| 连接池 | 单库单连接，无需池 |

### 迁移方案（内联自省）

```python
def _migrate(self) -> None:
    c = self._conn.execute("PRAGMA table_info(my_table)")
    cols = {row[1] for row in c.fetchall()}
    
    # 新增列
    if "new_column" not in cols:
        self._conn.execute("ALTER TABLE my_table ADD COLUMN new_column TEXT")
    
    # 检查 FTS 分词器版本
    if self._has_old_fts_tokenizer():
        self._rebuild_fts()
    
    # 迁移存量数据到新索引
    self._population_migration()
    
    self._conn.commit()
```

**优点**：零外部依赖，应用启动自动生效，适合单机部署场景。

---

## 六、关键模式总结

| 模式 | Akashic 的实践 | 你的项目参考 |
|------|----------------|-------------|
| **分库** | 5 个独立 SQLite DB | 按关注面拆分，不单库巨表 |
| **source_ref** | `"session:seq"` → 贯穿全链路 | 设计你自己的 source_ref 格式 |
| **幂等写入** | `content_hash + memory_type` UNIQUE | 防止重复记忆的关键 |
| **替换审计** | `memory_replacements` 保留前后快照 | 任何数据替换都要留审计痕迹 |
| **异步遥测** | asyncio.Queue + 后台消费者 | 可观测性不阻塞主链路 |
| **内联迁移** | PRAGMA table_info + ALTER TABLE | 小团队无需迁移框架 |
| **无 ORM** | 裸 SQL + DAO | 简单可控，每查询可优化 |
| **事件驱动** | EventBus 三模式：emit/fanout/enqueue | 解耦核心逻辑和可观测性 |

---

## 七、验证方式

1. **功能验证**：跑一轮完整对话 → 检查 5 个 DB 的数据是否按预期写入
2. **追溯验证**：给定一条消息 ID，正向找到所有关联记忆；给定一个记忆条目，反向找到原始消息
3. **审计验证**：对记忆执行 supersede，检查 `memory_replacements` 是否有完整新旧快照
4. **性能验证**：`trace.db` 异步写入队列在 500 QPS 下是否 drop，`state.db` WAL 模式下的并发表现
5. **迁移验证**：版本升级后启动，检查内联迁移是否自动生效
