# Cogito 与 memU 记忆系统对比分析

> 分析日期：2026-07-11
>
> 对比对象：Cogito 当前仓库的权威架构文档与实际代码；`reference/memU` 当前检入版本
>
> 文档性质：技术分析报告，不是 Cogito 的新增规范，也不改变现有设计契约

## 技术摘要

Cogito 和 memU 都在解决“如何让 Agent 跨交互保留并召回有用信息”，但二者的系统定位不同，因此不能简单按功能多少判断优劣。

- **Cogito 是完整 Agent 运行时中的记忆子系统。** 它明确区分 Session 内短期上下文与跨 Session 长期记忆，并把 Message、SessionSummary、ContextSnapshot、MemoryItem、来源、状态机、作用域和审计统一放进本地 SQLite 事实源。
- **memU 是可嵌入的长期记忆编译与检索库。** 它把对话、文档、媒体、代码和 Agent trace 编译为 Resource、RecallEntry、RecallFile、Segment 及可导出的 Markdown 文件树；短期消息窗口、会话状态和上下文快照主要由宿主 Agent 框架负责。
- **短期转长期的核心差异是“运行时晋升”与“显式摄取编译”。** Cogito 在 Turn 后异步读取当前 Session 消息、提取候选、按显式性确认并保留冲突链；memU 由调用方显式执行 `memorize()` 或 `memorize_workspace()`，通过预处理、LLM 提取/文件合成和持久化形成可检索记忆。
- **Cogito 更强调事实治理，memU 更强调内容组织与多模态编译。** Cogito 拥有 candidate/confirmed/rejected/expired、supersedes/contradicts、确认者、有效期、软删除、作用域隔离和衰减；memU 拥有多模态 Resource、分层文件/Segment、memory/skill 双轨、文件夹增量同步及 SQLite/Postgres/in-memory 后端。
- **当前实现成熟度呈互补关系。** Cogito 的核心记忆状态机、数据库、提取、摘要和检索已有测试覆盖，但部分高级算法与规范描述仍有偏差；memU 的摄取、向量检索、文件导出和多后端更通用，但缺少 Cogito 式的确认状态机、冲突/替代语义、统一短期上下文和治理闭环，并且其 README 明示项目正处于大规模重构期。

## 1. 范围、术语与判断口径

### 1.1 本报告所称“短期记忆”

短期记忆是当前交互连续性所需、默认不跨 Session 共享的内容，包括：

- 当前 Session 的近期 Message；
- 对较旧消息的滚动摘要；
- 当前 Turn/Attempt 使用的上下文选择结果；
- 临时事实、工具状态和 Checkpoint。

Cogito 对这些对象有明确规范和持久化实现。memU 本身不拥有完整的短期会话模型；当它接入 LangGraph 等宿主时，短期消息和线程状态通常属于宿主。

### 1.2 本报告所称“长期记忆”

长期记忆是可跨 Session 召回、具备稳定标识或持久内容、可追踪来源的信息。Cogito 的主要单位是 `MemoryItem`；memU 的主要持久单位是 `Resource`、`RecallEntry`、`RecallFile`、`RecallFileSegment` 及它们的关系。

### 1.3 证据等级

本报告采用三层证据：

1. **Cogito 权威规范**：以 `architecture > functional-spec > implementation-spec` 的顺序解释边界。
2. **实际代码与测试**：用于确认哪些设计已经落地，以及实现是否与规范一致。
3. **memU 自身文档、ADR 与源码**：源码优先于可能滞后的 README；README 已明确警告 API 和文档在 2026-07-15 前后仍可能变化。

## 2. 总体架构差异

| 维度 | Cogito | memU | 影响 |
|---|---|---|---|
| 产品定位 | 主动式个人 Agent 的完整运行时 | 可嵌入的记忆 SDK/编译与检索层 | Cogito 负责会话到执行闭环；memU 依赖宿主提供 Agent 生命周期 |
| 短期记忆所有者 | Cogito Core/Runtime | 宿主 Agent 或应用 | memU 不能单独替代 Cogito 的 Session、顺序和快照机制 |
| 长期记忆主单位 | 独立、原子化 `MemoryItem` | Resource、RecallEntry、RecallFile、Segment 多层表示 | Cogito 偏事实治理；memU 偏内容编译和分层导航 |
| 权威存储 | SQLite 是唯一事务事实源；Markdown 是派生视图 | in-memory、SQLite 或 Postgres；Markdown 文件树是可导出/可读表示 | Cogito 更强一致性；memU 更强部署可选性 |
| 写入触发 | Turn 后异步自动提取 + 显式记忆工具/服务 | 调用方显式 `memorize` 或 `memorize_workspace` | Cogito 更自然融入持续对话；memU 更适合批量/多模态摄取 |
| 读取触发 | 每 Turn 隐式召回 + `recall_memory` 显式工具 | `retrieve` 或 `retrieve_workspace` 显式调用 | Cogito 自动装配 prompt；memU 返回上下文给宿主决定如何注入 |
| 治理模型 | 状态机、来源、Scope、确认、冲突、替代、衰减、审计 | Scope 字段、来源链接、内容哈希、删除级联、可选 salience/reinforcement | Cogito 的事实生命周期更严格 |
| 多模态 | 消息 Asset 与派生 VisionAnalysis，长期记忆仍归一为 MemoryItem | 原生摄取 conversation/document/image/video/audio 等 Resource | memU 的通用多模态记忆编译更成熟 |
| 人工可读层 | `MEMORY.md` 等由数据库派生且可重建 | `INDEX.md`、`MEMORY.md`、`SKILL.md` 与 topic 文件是核心可浏览产物 | 两者都支持文件可读性，但权威性和生成路径不同 |

## 3. 短期记忆：Cogito 是内建系统，memU 是宿主责任

### 3.1 Cogito 的短期记忆模型

Cogito 把 Session 定义为短期上下文边界，而不是平台 Conversation。Session 由 Channel、平台 Conversation、可选 Thread、发送者和 reset generation 解析。不同 Session 不共享近期消息、Conversation Summary 或 Context Snapshot；跨 Channel 连续性只能通过长期 Memory 或显式 Task 实现。

短期上下文主要由四部分组成：

1. `messages`：带 `session_id`、稳定 `receive_sequence` 和内容部件的原始消息；
2. `session_summaries`：覆盖固定消息序列范围、可滚动更新、带版本和父摘要的结构化摘要；
3. `context_snapshots` / `context_snapshot_items`：某个 Attempt 实际选择的上下文、分数、Token、信任标签和检索路径；
4. Turn/Task Checkpoint 和 session-local 临时状态。

`ContextSnapshot` 是不可变选择结果。新消息到达不能修改旧 Snapshot；重试可复用，Session 版本变化或等待过久则创建带父引用的新 Snapshot。这使一次推理“看见了什么”可回放。

对应规范：

- `DOMAIN-CONTRACTS / 1.7 Session`
- `SESSION-CONTEXT / 3. 短期上下文`
- `RETRIEVAL-CONTEXT / 10. Context Snapshot`
- `MESSAGE-PERSISTENCE / Session Summary`（消息与摘要持久化部分）

### 3.2 memU 的短期记忆边界

memU 可以把 Conversation 当成一种输入模态进行预处理和长期记忆提取，但没有 Cogito 式的 Session 聚合、receive sequence、滚动短期摘要、Context Partition Lane 或每次 Attempt 的不可变 Context Snapshot。

其 LangGraph 集成只暴露：

- `save_memory(content, user_id, metadata)`：把内容写到临时文件后调用 `MemoryService.memorize()`；
- `search_memory(query, user_id, ...)`：调用 `MemoryService.retrieve()`。

因此，LangGraph 的 message state/checkpointer 才是短期记忆；memU 是被工具调用的长期存取层。调用方若不主动保存对话片段，短期内容不会自动进入 memU。

### 3.3 直接结论

若目标是“当前对话不要丢上下文、重试时可复现、不同群聊用户不串线”，Cogito 的模型更完整。若目标是“把大量既有对话、文档和媒体批量编译成可检索知识”，memU 的输入面更强。

## 4. 长期记忆的数据模型

### 4.1 Cogito：带生命周期的事实对象

Cogito 的 `MemoryItem` 不是一段无结构文本，而是：

```text
principal + kind + subject + predicate + value
+ scope + source + confidence + importance
+ status + valid_from/valid_to + supersedes_id
+ retrieval/reinforcement/decay fields
+ goal-specific state
```

主要 kind 为 `fact`、`preference`、`episode`、`goal`、`constraint`。状态为 `candidate`、`confirmed`、`rejected`、`expired`。检索默认只读取 confirmed、未删除、未过期且未被替代的条目。

优势：

- 可以确认或拒绝模型推断；
- 新值不必覆盖旧值，可建立 supersedes/contradicts 关系；
- 可按 Principal 和 Scope 做安全过滤；
- 可单独管理目标状态；
- 能保留来源、确认方法和审计语义。

代价：模型和写入路径更复杂；原始长文、媒体和项目知识需要先归一成 MemoryItem 或通过外部上下文源使用。

### 4.2 memU：Resource—Entry—File—Segment 分层表示

memU 当前模型包含：

- `Resource`：原始输入的 URL、模态、本地路径、caption、embedding、track；
- `RecallEntry`：原子记忆摘要、memory type、embedding、发生时间和扩展字段；
- `RecallFile`：主题记忆文件或技能文件，`track=memory|skill`，含描述、正文和 embedding；
- `RecallFileSegment`：RecallFile 的向量检索切片；
- `RecallFileEntry` / `RecallFileResource`：条目或来源与文件的关系。

默认 EntryType 为 `profile`、`event`、`knowledge`、`behavior`、`skill`、`tool`。这比 Cogito 的用户事实模型更适合保留技能、工具经验、知识文档和多模态资源。

但当前核心模型没有与 Cogito 等价的一等字段：

- candidate/confirmed/rejected/expired；
- confirmed_by / confirmation_method；
- valid_from / valid_to；
- supersedes / contradicts；
- 统一的 confidence、importance 和 deletion tombstone 语义。

部分 reinforcement、content hash、tool memory 统计被放在 `RecallEntry.extra` 中，灵活但契约和查询治理弱于一等字段。

## 5. 记忆持久化与文件视图

### 5.1 Cogito：数据库权威，Markdown 只读派生

Cogito 的设计要求 SQLite 是唯一事务事实源：

- `memory_items` 保存完整事实和生命周期；
- `memory_relations` 保存 supports/contradicts/supersedes/refines/derived_from；
- `memory_embeddings` 保存派生向量；
- FTS5 保存派生全文索引；
- Message、SessionSummary 和 ContextSnapshot 也在同一数据库体系内。

`MEMORY.md`、`PENDING.md`、`HISTORY.md` 等文件只是人工可读投影。数据库先提交，Markdown 可滞后、可重建，文件错误不能反向污染事实源。

这套设计的重点是事务、恢复和审计，而不是把文件本身当数据库。

对应规范：

- `MEMORY-LIFECYCLE / 1. 双层存储架构`
- `MEMORY-LIFECYCLE / 11. Markdown 视图与 Prompt Cache 保护`
- `STORAGE-DATA / SQLite 事实源与派生索引`
- `DATABASE-SCHEMA / Memory tables`

### 5.2 memU：可插拔结构化存储 + 可浏览文件系统

memU 支持：

- in-memory：测试或临时运行；
- SQLite：Embedding 以 JSON 文本存储，暴力余弦检索；
- Postgres：可使用 pgvector；
- 本地 blob/file 处理和 Markdown 导出。

其文件树通常为：

```text
INDEX.md
MEMORY.md
SKILL.md
resource/
memory/<topic>.md
skill/<name>.md
```

结构化数据库仍是服务内部检索和关系的基础，文件树由 exporter/builder 生成。确定性索引和每 topic 文件可重建；启用 synthesis 时，根概览可由 LLM 合成。`memorize_workspace()` 使用 `.memu_manifest.json` 做目录 diff，修改或删除来源时级联删除旧 Resource/Entry/关系并重新摄取。

与 Cogito 相比：

- memU 的后端和导出能力更通用；
- Cogito 的数据库权威规则、状态迁移和运行时一致性更严格；
- memU 的文件树是 Agent 导航和产品体验的重要组成；Cogito 的 Markdown 更偏运维/人工审阅视图。

## 6. 短期记忆如何转为长期记忆

### 6.1 Cogito：Turn 后异步提取与治理式晋升

当前实现路径为：

```text
Session messages committed
→ Turn 完成后异步触发 MemoryExtractor
→ 最少 4 条消息，最多读取最近 50 条
→ 主模型按结构化 Schema 提取 fact/preference/constraint/goal/episode
→ SqliteMemoryService.propose()
→ canonical key 查重与冲突处理
→ explicit_user_statement 直接 confirmed
→ model_inference 保持 candidate
→ 写 memory_items / relations
→ 后台补 embedding，后续 Context Builder 可召回 confirmed 项
```

这一流程的本质不是把整个 Summary 复制到长期库，而是从短期消息中提取可治理的原子事实。Conversation Summary 明确属于短期压缩，不自动等于长期 MemoryItem。

值得注意的实现事实：

- 当前 `MemoryExtractor` 的来源写为 `source_type="extractor"`、`source_id="auto_extract"`，没有精确保存每条源 Message ID；这弱于规范中“必须保留来源”的目标。
- 提取器通过消息数量阈值触发，而不是基于 token、Session 结束或语义变化自适应触发。
- 显式用户陈述会自动 confirmed；对敏感信息和高风险偏好的更细 Policy 仍需结合上层策略核验。

### 6.2 memU：显式 memorize 摄取与内容编译

memU 有两条主要写入路径。

**单资源 `memorize()`：**

```text
resource URL + modality
→ ingest
→ multimodal preprocess
→ 按 memory type 用 LLM 提取 RecallEntry
→ dedupe/merge（当前文档称该阶段仍是占位透传）
→ category routing
→ persist Resource/Entry/File/relations/embeddings
```

**目录 `memorize_workspace()`：**

```text
scan folder + manifest diff
→ 删除/修改来源的旧记忆级联清理
→ 每个文件预处理并创建 Resource
→ chat 文件合成为 memory-track RecallFile
→ agent trace 合成为 skill-track RecallFile
→ workspace 普通文件保留为 resource context
→ 创建 Segment 和 embedding
→ 可选重建 Markdown 文件树
```

这更像 ETL/编译，不是 Cogito 式“短期状态自动晋升”。宿主可在每轮、会话结束、人工指令或批处理时调用它；调用时机本身不由 memU 的 Session 生命周期控制。

### 6.3 晋升机制对比

| 问题 | Cogito | memU |
|---|---|---|
| 谁触发 | Runtime 在 Turn 后异步触发，也可显式工具写入 | 应用/Agent/CLI 显式调用 |
| 输入 | 当前 Session 已持久化消息 | 文件、对话、文档、媒体、URL、trace |
| 转换单位 | 原子 `MemoryItem` 候选 | Entry、主题 File、Segment、Resource |
| 是否有候选状态 | 有 | 无等价核心状态 |
| 用户确认 | 有 confirm/reject API 和字段 | 需应用层自行实现 |
| 冲突处理 | canonical key + supersedes/contradicts | 内容哈希/分类/合成；无同等级事实冲突状态机 |
| 来源 | 规范要求精确来源，当前自动提取实现仍偏粗 | Resource 与 File/Entry 关系可回溯，workspace 文件可列 resource URL |
| 失败影响 | 提取失败不阻塞用户回复 | memorize 调用失败由调用方处理；文件导出失败不回滚已持久化结构化记忆 |

## 7. 检索与上下文注入

### 7.1 Cogito：隐式 + 显式双路径

Cogito 每个 Turn 自动构建 Context Snapshot，同时提供 `recall_memory` 工具补充深挖。规范目标是 FTS/BM25 与向量双路召回，并综合 recency、importance、source quality、goal relevance 等排序，再按 Token Budget 装配近期消息、摘要、长期记忆、目标和外部上下文。

关键治理发生在排序之前：Principal、Scope、status、validity、trust、superseded 都是硬过滤。即使共享群聊短期 Session，也不能因此共享 Owner 的私有长期记忆。

### 7.2 memU：分层检索

memU 的 `retrieve()` 支持 RAG 或 LLM 两种流水线：意图路由/查询改写、文件召回、充分性判断、Entry 召回、Resource 召回和上下文构建。RAG 主要依赖 embedding，也可对 Entry 使用 salience/recency。

`retrieve_workspace()` 是更简单的零聊天 LLM 路径：query 只 embedding 一次，先对 Segment 做余弦排序，再汇总到 File，同时检索 workspace Resource。它不做意图路由、查询改写或充分性判断。

### 7.3 检索能力的实质差异

- Cogito 的优势是把短期和长期来源统一装入一次可审计 Snapshot，并在运行时自动执行。
- memU 的优势是对编译后的主题文件、细粒度 Segment、原始 Resource 和 skill track 做跨模态、跨内容类型检索。
- Cogito 的 FTS + vector 双路更适合人名、ID、日期等精确项与语义项混合召回；memU 当前 workspace 快速路径主要是向量相似度。
- memU 返回上下文，但不决定宿主 Prompt 的 token 配额、消息顺序或快照复用；这些仍需集成层实现。

## 8. 更新、冲突、强化、衰减与遗忘

### 8.1 Cogito

规范设计包含：

- 新事实写新对象，不原地抹去历史；
- `supersedes`、`contradicts`、`refines` 等关系；
- 被动召回只增加 exposure/retrieval count，不自动强化；
- 用户确认或成功任务依赖才增加 reinforcement；
- 按 kind 使用不同衰减率；
- 低权重先退出默认检索，再进入遗忘候选；
- 用户明确删除才清理 embedding/派生视图，并保留最小 tombstone。

实际代码已经实现状态迁移、软删除、confirmed 检索、替代关系、被动召回计数、基础 maintenance 和 embedding 表。但应注意：

- `MemoryRepository.apply_decay()` 当前采用乘法衰减及固定下限，和规范中的指数公式、归档阈值、分 kind 半衰期并不完全一致；
- 规范说 consolidation 写新 MemoryItem 合并 Episode，实际实现是否完整覆盖该后台任务需继续按调度路径验证；
- 规范 `RETRIEVAL-CONTEXT / 11.1` 说普通 recall 不强化，而 `11.2` 又说每次工具调用 reinforcement +1，文档内部存在矛盾；代码注释倾向“被动召回不强化”。

### 8.2 memU

memU 具备：

- `compute_content_hash()` 对 memory type + 规范化 summary 做哈希；
- Entry `extra` 可承载 reinforcement_count、last_reinforced_at、ref_id 等；
- RAG 路径可使用 salience 和 recency decay；
- workspace sync 对修改/删除来源执行级联清理；
- File/Resource 关系提供来源回溯。

但它当前没有统一的一等事实生命周期：召回项不会经历 candidate → confirmed，旧事实也没有标准 supersedes 链。LLM 合成 RecallFile 时更接近“更新主题文档”，治理粒度与 Cogito 不同。

已知实现限制也很重要：memU 架构文档指出 dedupe_merge 仍是占位；skill RecallFile 绕过 Entry 平面，来源变更/删除时无法可靠失效，当前是 append/merge-only 的已知缺口。

## 9. 安全、隔离与可审计性

### 9.1 Cogito

Cogito 的隔离根是 Principal，并叠加 global/user/conversation/session/task 等 Scope、Trust Label 和 Channel/Conversation 边界。短期历史严格限定当前 Session；长期记忆按当前消息发送者做 Principal 过滤。Context Snapshot 记录实际选择项和排除摘要，支持解释一次推理输入。

这是面向群聊、多 Channel 和主动推送 Agent 的必要设计，尤其能避免“共享 Session 等于共享私人长期记忆”的错误。

### 9.2 memU

memU 通过可配置 `user_model` 把 user_id 等 scope 字段合并进 Resource、Entry、File、关系和 Segment 模型，并在 `where` 中验证过滤字段。其通用性很高，可由业务定义 tenant/user/project/session 等隔离字段。

不过，隔离是否每次都正确传入 `where` 是调用方责任；memU 没有 Cogito 的 Principal/Endpoint/Conversation 安全模型，也没有自动从当前入站消息推导安全 Scope 的完整链路。

## 10. 实现成熟度与验证结果

### 10.1 Cogito 已验证部分

本次在 `conda activate cogito` 后运行以下聚焦测试：

```text
tests/domain/test_memory.py
tests/store/test_memory_repo.py
tests/service/test_memory_extractor.py
tests/service/test_memory_service.py
tests/service/test_summary_service.py
tests/service/test_memory_views.py
tests/integration/test_memory_e2e.py
```

结果：**91 passed，耗时 15.55 秒**。

这说明当前仓库的 MemoryItem 领域对象、Repository、候选提取、服务、Session Summary、Markdown view 和基础端到端链路在现有测试范围内可运行。它不证明真实模型质量、长期衰减的时间尺度效果或生产负载下的并发性能。

### 10.2 memU 当前稳定性判断

本报告对 memU 做源码和文档静态核验，没有调用真实模型或运行其完整测试套件，原因是用户请求是架构与实现比较，且 memU 默认模型路径可能产生外部调用成本。其 README 明示项目处于大规模重构期，API、CLI 和文档可能变化，因此本报告结论只适用于当前检入版本。

另有一处文档漂移：memU README 声称有 CLI，而 `docs/architecture.md` 的 Integration surfaces 段仍写“没有内建 HTTP server 或 CLI”；当前仓库实际存在 `src/memu/cli.py` 和 npm launcher，应以源码/README 的较新事实为准。

## 11. 哪些 memU 思路值得 Cogito 吸收

### 11.1 建议吸收：Resource—File—Segment 的内容层

Cogito 不应把 memU 整套长期记忆模型直接替换 `MemoryItem`，但可以增加一个与事实记忆并列的“可检索内容层”：

```text
MultimodalAsset / ExternalResource
→ normalized resource text/caption
→ topic document or knowledge file
→ searchable segments
→ source links
```

它适合项目文档、PDF、代码仓库、长日志和媒体，不必强行拆成用户 fact/preference。`MemoryItem` 继续负责个人事实、偏好、目标、约束和 Episode。

### 11.2 建议吸收：目录增量同步与来源删除级联

memU 的 manifest diff、只处理新增/修改文件、删除来源时级联失效，是 Connector/Workspace 摄取的实用模式。Cogito 可将其实现为 Connector + Task + Event/Outbox，而不是让文件扫描器直接写数据库。

设计时至少检查：

- `CONNECTOR-INGESTION`
- `EVENT-OUTBOX`
- `TASK-SCHEDULER`
- `MEMORY-LIFECYCLE`
- `DOMAIN-CONTRACTS`
- `DATABASE-SCHEMA`

### 11.3 建议吸收：memory 与 skill 双轨

memU 将个人/知识记忆和可复用技能分开检索，方向是正确的。Cogito 若引入 Skill 学习，不应把 Skill 伪装成 `MemoryItem(kind=fact)`；应让 Capability/Plugin/Skill 子系统拥有 Skill，Memory 只保存来源 Episode、效果证据和用户偏好，再用显式关系连接。

### 11.4 不建议直接照搬

- 不应弱化 Cogito 的 candidate/confirmed 和冲突链；
- 不应让 Markdown 成为可绕过数据库服务的写入口；
- 不应把 user_id `where` 完全交给调用方；
- 不应把所有消息自动批量编译为长期记忆，避免噪声、隐私和错误归因；
- 不应在缺少来源关系时自动生成可执行 Skill。

## 12. Cogito 当前最值得优先修补的记忆问题

### P0：让自动提取保留精确来源

当前 `source_id="auto_extract"` 无法回答“这条记忆来自哪几条 Message”。建议提取输入携带 message_id/sequence，写入 `memory_sources` 或明确的 relation 表；一次候选可以关联多条 Message，且删除/纠正源消息时能重新评估。

依据：`DOMAIN-CONTRACTS / 1.13 MemoryItem`、`MEMORY-LIFECYCLE / 7. 写入流程`。

### P0：统一规范与衰减实现

把规范公式、Repository 的乘法衰减、归档阈值、candidate 过期和 reinforcement 触发整理成一个版本化算法，并用固定时钟测试边界。否则文档中的半衰期和实际检索权重不可互相解释。

依据：`MEMORY-LIFECYCLE / 4. 检索权重算法`、`5. 衰减速率与遗忘策略`。

### P1：消除 recall reinforcement 的规范矛盾

明确以下三类事件：展示、Agent 工具主动检索、用户确认引用。建议前两者只增加 exposure/retrieval，只有用户确认或成功 Task 证据增加 reinforcement，避免“越误召回越强”。

依据：`MEMORY-LIFECYCLE / 5.3 Reinforcement 规则` 与 `RETRIEVAL-CONTEXT / 11. recall_memory 工具`。

### P1：补齐长文档/项目知识的 Resource—Segment 层

将 memU 的分层表示作为参考，新增知识资源与 Segment，不要扩大 MemoryItem 责任。检索时由 Context Builder 合并 MemoryItem、SessionSummary、ResourceSegment，并继续生成统一 Context Snapshot。

### P1：把晋升触发从固定消息数升级为策略

当前 4 条消息阈值简单可用，但可增加：Session 结束、token 水位、用户“记住”指令、目标/约束检测、重大决策、后台 idle 等触发条件。所有触发仍写幂等 Task/Event，不能阻塞即时回复。

### P2：增加 memU 式来源变更/删除回放测试

测试“源 Message/Resource 修改或删除后”：候选、confirmed 事实、Embedding、FTS、Markdown view 和 Context Snapshot 后续行为是否符合政策，防止索引重建让已删内容复活。

## 13. 推荐的组合架构

若未来希望同时获得两者优势，推荐保持三层而不是二选一：

```text
短期运行时层（Cogito 拥有）
  Message + SessionSummary + Checkpoint + ContextSnapshot

事实记忆层（Cogito MemoryItem 拥有）
  fact/preference/episode/goal/constraint
  candidate/confirmed + scope + source + conflicts + decay

内容与技能层（借鉴 memU，但由各自服务拥有）
  Resource + TopicFile + Segment + provenance
  Skill 归 Capability/Skill 子系统，不归 MemoryItem
```

统一检索可以跨三层召回，但写入权必须分离：

- Session 服务写短期对象；
- Memory 服务写事实记忆；
- Resource/Connector 服务写内容资源；
- Skill/Capability 服务写可执行或提示型技能；
- Context Builder 只读并生成不可变 Snapshot。

这与 Cogito 的模块边界规则一致，也避免从 memU 引入一个旁路数据库直接改变 Agent 状态。

## 14. 限制、不确定性与后续问题

1. 本报告没有运行 memU 的真实 LLM/VLM/Embedding 流水线，因此不评价其抽取准确率、成本和 benchmark 可复现性。
2. memU 正处于重构期，legacy `memorize()` 与新 `memorize_workspace()` 的数据平面并存；后续版本可能继续减少 RecallEntry 在 workspace 路径中的角色。
3. Cogito 权威文档描述了完整目标态，部分后台 consolidation、自动归档、Markdown 刷新调度和向量索引任务仍需沿 Worker/Task 调度路径做专项运行验证。
4. 两者的 embedding 质量、中文分词、真实数据规模和隐私政策没有统一基准，不能仅凭代码结构判断召回效果。
5. 若要决定是否把 memU 作为 Cogito 的依赖，还需做一次受控 PoC：同一批中文对话、项目文档和矛盾偏好数据，比较 recall@k、错误记忆率、来源可追踪率、删除完整性、token 成本和延迟。

## 15. 最终判断

**memU 不适合作为 Cogito 全部记忆系统的直接替代品。** 它没有覆盖 Cogito 所需的 Session 短期上下文、执行快照、Principal 安全边界、候选确认和事实冲突治理。

**memU 很适合作为 Cogito“内容型长期记忆/知识资源编译”能力的设计参考，甚至可作为隔离的适配后端进行 PoC。** 最值得借鉴的是多模态 Resource、Topic File + Segment、目录增量同步、来源链接、memory/skill 分轨和可浏览文件树。

**Cogito 应继续以自己的 SQLite、MemoryService 和 Context Builder 为权威链路。** 若集成 memU，推荐通过 Connector/Task 或新的 Resource 索引适配器接入，让 memU 产物以外部派生上下文进入检索；任何个人事实的确认、冲突、删除和 Scope 仍必须回到 Cogito MemoryService。

## 16. 主要证据索引

### Cogito 权威规范

- `DOMAIN-CONTRACTS / 1.7 Session`、`1.13 MemoryItem`
- `SESSION-CONTEXT / 3. 短期上下文`、`4. 长期认知共享`、`6. Session 重置`
- `RETRIEVAL-CONTEXT / 1. 两条检索路径`、`4. 检索源`、`10. Context Snapshot`、`11. recall_memory 工具`
- `MEMORY-LIFECYCLE / 1. 双层存储架构`、`6. 候选产生`、`7. 写入流程`、`10. Consolidation`、`11. Markdown 视图与 Prompt Cache 保护`、`14. 纠正与删除`
- `MESSAGE-PERSISTENCE`（Message、Revision、SessionSummary 的持久化与历史规则）
- `STORAGE-DATA`（SQLite 权威事实源与派生索引）
- `DATABASE-SCHEMA`（memory、summary、snapshot 相关表）

### Cogito 实现

- `src/cogito/domain/memory.py`
- `src/cogito/service/memory_extractor.py`
- `src/cogito/service/memory_service.py`
- `src/cogito/service/retrieval_service.py`
- `src/cogito/service/summary_service.py`
- `src/cogito/store/memory_repo.py`
- `src/cogito/store/context_snapshot_repo.py`
- `src/cogito/store/schema.py`
- `src/cogito/store/migrations/0012_memory_mvp.sql`
- `src/cogito/store/migrations/0014_fts5_memory.sql`
- `src/cogito/store/migrations/0015_memory_lifecycle.sql`
- `src/cogito/store/migrations/0016_memory_reliability.sql`
- `src/cogito/store/migrations/0019_memory_lifecycle.sql`

### memU 文档与实现

- `reference/memU/README.md`
- `reference/memU/docs/architecture.md`
- `reference/memU/docs/sqlite.md`
- `reference/memU/docs/adr/0002-pluggable-storage-and-vector-strategy.md`
- `reference/memU/docs/adr/0003-user-scope-in-data-model.md`
- `reference/memU/docs/adr/0004-workspace-memorize-and-memory-file-system.md`
- `reference/memU/docs/adr/0006-from-memory-item-category-to-tracked-workspace-memorization.md`
- `reference/memU/docs/adr/0007-three-independent-memory-lines-wiki-graph.md`
- `reference/memU/src/memu/app/memorize.py`
- `reference/memU/src/memu/app/memorize_workspace.py`
- `reference/memU/src/memu/app/retrieve.py`
- `reference/memU/src/memu/app/retrieve_workspace.py`
- `reference/memU/src/memu/database/models.py`
- `reference/memU/src/memu/database/interfaces.py`
- `reference/memU/src/memu/integrations/langgraph.py`
- `reference/memU/src/memu/memory_fs/`
