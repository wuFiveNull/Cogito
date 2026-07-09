# 08 Dashboard 需求与设计文档

> 状态：draft  
> 目标读者：前端、Interaction API、后台任务、主动系统、运维治理开发者  
> 当前依据：本地仓库 `markdown/` 设计文档、`src/cogito/interaction_web`、`web/src`、`plan/01-07`  
> 设计原则：Dashboard 只读查询走 Query API；所有写操作走 Command API；实时流只作观察通道，断线后必须可由 Query API 重同步。

## 1. 设计目标

Dashboard 是 Cogito 的本地控制面和运行观察面，不只是数据展示页。它要回答三个问题：

1. Agent 现在是否健康？
2. Agent 为什么做了某个决定、为什么没做、卡在哪里？
3. Owner 可以安全地暂停、重试、确认、拒绝、回放或调整策略吗？

Dashboard 的第一版应覆盖以下核心场景：

1. 用户即时消息链路：Conversation → Session → Message → Turn → RunAttempt → ModelCall → Delivery；
2. 后台任务链路：Task → TaskAttempt → Lease → Checkpoint → Outcome；
3. 主动系统链路：Connector/Event → Candidate → ProactiveDecision → ScheduledDelivery/Digest → Delivery → Feedback；
4. 能力系统链路：Tool/MCP/Skill/Plugin → Policy → Receipt → Reconcile；
5. 存储和治理链路：SQLite、Payload、Config、Backup、Restore、Trace、Audit、ResourceBudget。

## 2. 设计依据

### 2.1 文档依据

- `SYSTEM-BOUNDARIES / 2. 组件边界`：`interaction-web` 只使用 Query/Command/Stream API。
- `SYSTEM-BOUNDARIES / 4. 状态所有权`：Dashboard 不直接执行写 SQL；其他模块通过 Command 或公开 Service 请求变更。
- `ACCESS-DELIVERY / 2. Interaction`：Dashboard、Web Channel、Query API、Command API、实时事件流属于 Interaction。
- `ACCESS-DELIVERY / 2.2 Query API`：Query API 只调用只读视图，不直接暴露数据库表结构。
- `ACCESS-DELIVERY / 2.3 Command API`：Command API 接受幂等键、expected_version、权限和审计信息；不允许 Dashboard 直接更新业务表。
- `ACCESS-DELIVERY / 2.4 实时流`：实时流是观察通道，不是唯一事实源；断线后通过 Query API 重同步。
- `PROACTIVE-TASKS / 4. 主动系统`：主动系统 dry-run 结果必须能在 Dashboard 展示“本应发送”的内容和原因。
- `PROACTIVE-IDLE / 5. 主动决策`：主动决策输出必须包含 `send_now | send_later | digest | silent | create_task | ask_permission | discard`。
- `PROACTIVE-IDLE / 9. Dry-run`：保存动作、内容预览、规则结果和预算估算，但不创建真实 Delivery 或外部副作用。
- `OBSERVABILITY-AUDIT / 5. Metric`：核心指标包括 Turn 延迟、模型 Token、Tool unknown、Task backlog、Outbox age、Delivery 成功、Connector freshness、Memory 命中、SQLite busy、Payload 增长、Drift 资源。
- `SECURITY-OBS / 4.5 Dashboard 观察范围`：Dashboard 展示 Approval、Command、Tool、Task、Delivery、Trace Tree、Payload 权限、资源预算和降级状态。
- `OPS-GOVERNANCE / 2.7 总体架构验收标准`：Dashboard 写操作全部通过 Command API，核心链路可以通过 Trace、Audit 和状态记录解释。
- `CONFIG-PROFILES / 4. Secret`：配置 dump、Dashboard 和 Trace 只显示 Secret 引用名，不显示值。
- `LOCAL-OPERATIONS / 8. 健康检查`：分别报告 liveness、readiness、SQLite/Payload、Provider、Gateway、队列 backlog 和降级原因。

### 2.2 当前代码基线

当前已有前端页面：

- `web/src/pages/Overview.tsx`
- `web/src/pages/Chat.tsx`
- `web/src/pages/ResourceList.tsx`，用于 Runs / Tasks
- `web/src/pages/Memory.tsx`
- `web/src/pages/Connectors.tsx`
- `web/src/pages/Channels.tsx`
- `web/src/pages/Trace.tsx`
- `web/src/pages/Deliveries.tsx`
- `web/src/pages/Commands.tsx`
- `web/src/pages/Plugins.tsx`

当前已有 Query API：

- `GET /api/status`
- `GET /api/usage`
- `GET /api/turns`
- `GET /api/turns/{turn_id}`
- `GET /api/turns/{turn_id}/attempts`
- `GET /api/tasks`
- `GET /api/tasks/{task_id}`
- `GET /api/memory`
- `GET /api/connectors`
- `GET /api/channels`
- `GET /api/conversations`
- `GET /api/conversations/{conversation_id}/messages`
- `GET /api/sessions`
- `GET /api/sessions/{session_id}/trace`
- `GET /api/deliveries`
- `GET /api/traces/{trace_id}`
- `GET /api/plugins`
- `GET /api/debug/trace/{conversation_id}`
- `GET /api/bench/last`
- `GET /api/bench/web_adapter_state`

当前已有 Command API：

- `POST /api/commands/cancel-turn`
- `POST /api/commands/retry-task`
- `POST /api/commands/approve`
- `POST /api/commands/reject`
- `POST /api/commands/confirm-memory`
- `POST /api/commands/delete-memory`
- `POST /api/commands/delete-session`
- `POST /api/commands/delete-sessions-by-conversation`
- `POST /api/commands/pause-connector`
- `POST /api/commands/disable-plugin`
- `POST /api/commands/replay-delivery`

当前前端风险：

- `web/src/api.ts` 在后端不可用或接口未实现时会自动回退 `mock.ts`。
- 该行为适合演示，但生产/真实运行时必须显式区分 `demo mode` 与 `real mode`，不能让用户误以为 mock 数据是真实状态。

## 3. Dashboard 用户与核心任务

### 3.1 主要用户

1. Owner：个人 Agent 的使用者和管理员。
2. Developer：调试本地运行链路、Connector、Tool、Channel、Plugin 的开发者。
3. Operator：同 Owner，强调本地运行、恢复、备份、资源和安全状态。

当前系统是单机个人使用，不设计多租户后台。远程访问不是本阶段目标。

### 3.2 主要任务

Owner 需要：

1. 查看 Agent 是否在线、是否可接收消息、是否能发送；
2. 查看最近对话、主动候选和待确认事项；
3. 确认或拒绝 Memory、Approval、主动通知；
4. 暂停 Connector、重放 Delivery、重试 Task；
5. 理解“为什么这条主动消息被发送/延后/丢弃”；
6. 查看资源预算、模型用量、失败和降级原因。

Developer 需要：

1. 追踪一个用户消息从入站到回复的完整链路；
2. 看到 Turn/RunAttempt/ModelCall/ToolCall/Delivery 的时间线；
3. 找出卡在 queued/running/waiting/unknown 的对象；
4. 查看 Outbox、Task、Delivery、Connector 的 backlog；
5. 比较 mock/demo 数据和真实 API 数据；
6. 查看 Payload/Trace 的脱敏和权限结果。

Operator 需要：

1. 查看 SQLite/Payload/Backup/Config 状态；
2. 查看启动恢复、Lease reclaim、unknown side effect；
3. 确认真实副作用是否被禁用、dry-run 是否开启；
4. 发现磁盘、队列、预算、Provider、Gateway 的健康风险。

## 4. 信息架构

Dashboard 分为 11 个一级页面：

1. 总览 Overview
2. 对话 Chat & Sessions
3. 运行 Runs
4. 任务 Tasks & Scheduler
5. 主动系统 Proactive
6. 投递 Deliveries
7. 数据源 Connectors & Events
8. 记忆 Memory
9. 能力 Capabilities
10. Trace & Audit
11. 系统 System

现有页面不需要推倒重写，但需要重新归类：

- `Overview` 保留为总览；
- `Chat` 合并对话与 Web Channel；
- `Runs` 保留 Turn/RunAttempt；
- `Tasks` 扩展 Scheduler、Outbox、Waiting；
- `Connectors` 扩展 Event、RawItem、Cursor、Freshness；
- `Deliveries` 扩展 Attempt、Receipt、Reconcile；
- `Trace` 扩展为 Trace & Audit；
- `Plugins` 并入 Capabilities；
- 新增 `Proactive` 与 `System` 两个关键页面。

## 5. 全局交互规则

### 5.1 全局状态栏

所有页面顶部显示：

1. 当前 Profile；
2. 数据模式：`real | demo | mock fallback`；
3. API 连接状态；
4. Worker 状态；
5. Scheduler 状态；
6. Gateway/Channel 状态；
7. Proactive 状态：`disabled | dry_run | live | degraded`；
8. 最近刷新时间；
9. 当前过滤器摘要。

如果处于 mock fallback，顶部必须使用高可见度提示：

> 当前展示演示数据，不代表真实 Agent 状态。

mock fallback 不允许执行 Command。

### 5.2 全局过滤器

默认过滤器：

- 时间窗口：`1h | 24h | 7d | 30d | custom`
- Principal：默认 `owner`
- Channel：`all | web | qq | langbot | other`
- 状态：`active | waiting | failed | unknown | completed | archived`
- 数据模式：`real only | include dry-run | dry-run only`

过滤器原则：

- 总览页只保留时间窗口、Principal、数据模式；
- 诊断页允许更多过滤器；
- 写操作弹窗不继承危险过滤器，必须明确显示目标对象 ID 和 expected_version。

### 5.3 刷新与实时

Dashboard 使用三层更新：

1. 页面初次加载：Query API 获取事实快照；
2. 页面打开期间：Stream API 或 WebSocket 接收状态变更；
3. 断线重连后：Query API 重新同步。

实时流不得作为唯一事实源。所有实时事件只用于局部刷新提示。

## 6. 页面设计

## 6.1 Overview：运行总览

### 目标

默认 10 秒内回答：系统是否健康、是否有需要 Owner 处理的事项、主动系统是否安全运行。

### 核心卡片

1. Agent 状态
   - profile
   - readiness
   - recovery 状态
   - worker concurrency
   - scheduler enabled
   - gateway/channel status

2. 注意事项 Inbox
   - pending approval
   - memory candidate
   - unknown delivery
   - dead letter event
   - failed task
   - connector paused/auth_failed
   - proactive dry-run pending review

3. 最近 24h 活动
   - turns
   - messages
   - tasks executed
   - deliveries attempted
   - proactive candidates
   - connector items ingested
   - memory changes

4. 质量与成本
   - model calls
   - input/output/cached tokens
   - average model latency
   - error count
   - delivery success rate
   - task retry rate

5. 主动系统摘要
   - enabled/dry_run/live
   - candidates queued
   - decisions by action
   - send_now/send_later/digest/silent/discard
   - daily budget used
   - quiet hours active

6. 资源摘要
   - SQLite size
   - Payload size
   - Trace retention
   - backup freshness
   - disk pressure

### 主要图表

1. 24h 事件时间线：Turn、Task、Delivery、Connector、ProactiveDecision。
2. 状态堆叠条：Turn/Task/Delivery 当前状态分布。
3. 模型用量折线：calls/tokens/latency/errors。
4. 主动决策分布：按 action 分组。

### 必须支持的操作

- 跳转到对应对象详情；
- 一键查看所有需要处理的事项；
- 暂停主动系统；
- 进入 dry-run review；
- 打开 Trace 诊断。

写操作必须走 Command API。

## 6.2 Chat & Sessions：对话与会话

### 目标

提供真实 Web Channel 使用入口，同时支持历史回放和会话诊断。

### 展示数据

- conversation list
- session list
- message timeline
- turn status
- reply delivery status
- streaming placeholder/edit/finalize 状态
- session reset_generation
- context_partition_key

### 功能

1. 新建 Web 对话；
2. 发送消息；
3. 查看历史消息；
4. 删除/归档 Session；
5. 从任一消息跳转到对应 Turn Trace；
6. 显示当前消息是否已创建 Delivery；
7. 显示流式回复是否降级为 final-only。

### 关键规则

- 消息内容事实源是 Message；
- 流式 Delta 不是 Message；
- Delivery 状态不能替代 Message；
- 删除会话必须是软删除并产生 Command/Audit。

## 6.3 Runs：Turn 与 RunAttempt

### 目标

解释即时交互执行链路。

### 列表字段

- turn_id
- session_id
- input_message_id
- status
- priority
- created_at
- next_attempt_at
- completed_at
- attempt_count
- latest_attempt_status
- worker_id
- lease_expires_at
- error_ref

### 详情页

Turn 详情展示：

1. 输入消息摘要；
2. Turn 状态机；
3. RunAttempt 列表；
4. Checkpoint 引用；
5. ModelCall 列表；
6. ToolCall 列表；
7. Memory Candidate 变更；
8. Delivery 引用；
9. TraceContext。

### 操作

- Cancel Turn；
- Retry Turn；
- Resume after Approval；
- Open Trace；
- View Checkpoint；
- Mark manual inspection complete。

所有操作必须携带 expected_version。

## 6.4 Tasks & Scheduler：后台任务

### 目标

解释后台任务是否堆积、是否卡住、是否可以安全重试。

### 列表分区

1. Active Tasks
2. Waiting Tasks
3. Failed / Retry Scheduled
4. Scheduled Fires
5. Outbox Pending
6. Dead Letter

### Task 字段

- task_id
- task_type
- status
- priority
- origin
- scheduled_at
- next_attempt_at
- lease_owner
- lease_version
- lease_expires_at
- attempt_count
- idempotency_key
- payload_ref
- checkpoint_ref
- config_version_id

### Scheduler 字段

- schedule_id
- schedule_type
- expression
- timezone
- misfire_policy
- next_fire_at
- last_fired_at
- normalized_interval_s
- dst_policy
- enabled

### 功能

- retry task；
- pause schedule；
- resume schedule；
- inspect checkpoint；
- view spawned child tasks；
- view waiting condition；
- replay outbox event；
- dead letter resolution command。

### 图表

- backlog by task_type；
- queued age p50/p95/max；
- retry count distribution；
- scheduler fires over time；
- dead letter by reason。

## 6.5 Proactive：主动系统

### 目标

这是新增核心页面。它必须让 Owner 清楚看到 Agent 主动性边界：

1. 为什么某条信息被候选；
2. 为什么发送、延后、加入摘要、静默或丢弃；
3. dry-run 下“本应发送”的内容；
4. 如何调整策略而不直接编辑内部状态。

### 页面结构

1. Proactive 状态栏
   - enabled
   - dry_run
   - default_principal_id
   - quiet_hours
   - hourly/daily budget
   - current energy value
   - current policy_version

2. Candidate Queue
   - candidate_id
   - principal_id
   - source_type
   - source_ref
   - topic
   - urgency
   - relevance_score
   - freshness_score
   - novelty_score
   - status
   - idempotency_key
   - created_at

3. Decision Log
   - decision_id
   - candidate_id
   - action
   - dry_run
   - rule_trace
   - model_score_json
   - energy_value
   - policy_version
   - decided_at

4. Dry-run Review
   - would_send preview
   - reason
   - target suggestion
   - budget impact
   - user feedback controls

5. Scheduled Delivery Requests
   - request_id
   - candidate_id
   - scheduled_at
   - status
   - topic
   - suggestion target
   - converted_delivery_id

6. Digest Buckets
   - digest_id
   - principal_id
   - topic
   - date
   - item_count
   - status
   - scheduled_at
   - sent_delivery_id

7. Feedback & Drift
   - opened
   - ignored
   - dismissed
   - useful
   - not_useful
   - muted
   - requested_more
   - drift preemption reason

### 必须支持的操作

- enable/disable proactive；
- switch dry_run/live；
- approve send_now from dry-run；
- dismiss candidate；
- send later；
- add to digest；
- mute topic；
- update quiet hours；
- update budget；
- open source item；
- open decision trace；
- import proactive context markdown。

### 安全要求

- live 模式切换必须二次确认；
- 群聊主动发送默认禁止，除非 policy 明确允许；
- 所有策略变更走 `UpdateProactivePolicy` Command；
- 人工编辑 `PROACTIVE_CONTEXT.md` 必须经 Import Command、Diff、版本检查和 Audit；
- dry-run 不能产生真实 Delivery。

## 6.6 Deliveries：投递与对账

### 目标

解释内容是否已安全发送，失败是否可重试，对 unknown 结果如何处理。

### 列表字段

- delivery_id
- message_id
- turn_id
- candidate_id
- status
- content_mode
- stream_status
- degradation_mode
- target_snapshot summary
- scheduled_at
- attempt_count
- lease_owner
- lease_version
- last_error
- created_at

### Attempt 字段

- attempt_id
- attempt_no
- status
- operation_seq
- platform_message_id
- receipt_kind
- request_hash
- lease_version
- started_at
- finished_at
- error_code

### 功能

- replay delivery；
- reconcile unknown；
- inspect target snapshot；
- inspect platform receipt；
- view related message；
- view related turn；
- view streaming operation sequence。

### 图表

- delivery success rate；
- delivery latency p50/p95；
- unknown count；
- retry count；
- failure reason distribution；
- streaming degradation mode distribution。

## 6.7 Connectors & Events：数据源、事件与 Outbox

### 目标

解释外部数据如何进入系统，是否新鲜，是否被去重、归档、候选化。

### Connector 字段

- connector_id
- connector_type
- name
- status
- source_uri
- last_success_at
- last_failure_at
- next_poll_at
- cursor summary
- etag/last_modified summary
- failure_count
- auth_status

### Ingestion 字段

- raw_item count
- normalized item count
- deduped count
- quarantined count
- candidate count
- latest batch_id
- batch status

### Event/Outbox 字段

- event_id
- event_type
- aggregate_type
- aggregate_id
- aggregate_version
- status
- consumer
- attempt_count
- next_attempt_at
- dead_letter reason

### 功能

- pause connector；
- resume connector；
- force poll；
- view cursor；
- view raw payload metadata；
- replay event dry-run；
- resolve dead letter；
- quarantine review。

### 必须补齐的视图

当前 API 只有 `/connectors`。应新增：

- `GET /api/connectors/{id}`
- `GET /api/connectors/{id}/items`
- `GET /api/connectors/{id}/batches`
- `GET /api/events`
- `GET /api/outbox`
- `GET /api/dead-letter`
- `POST /api/commands/replay-event`
- `POST /api/commands/force-connector-poll`

## 6.8 Memory：长期记忆

### 目标

让 Owner 知道系统记住了什么、为什么记住、是否需要确认、是否被检索使用。

### 页面分区

1. Confirmed Memory
2. Candidate Memory
3. Conflicts
4. Goals
5. Retrieval Activity
6. Embedding / Index 状态

### 字段

- memory_id
- kind
- status
- scope_type
- scope_id
- subject
- predicate
- value
- confidence
- importance
- explicitness
- source
- confirmed_by
- confirmed_at
- retrieval_count
- last_retrieved_at
- version
- deleted_at

### 功能

- search memory；
- confirm candidate；
- reject candidate；
- edit via command；
- soft delete；
- show source evidence；
- show retrieval path；
- show conflict group；
- rebuild derived markdown；
- rebuild embedding。

### 图表

- memory count by kind/status；
- candidate age；
- retrieval hits over time；
- stale/expired memory count；
- conflict count。

## 6.9 Capabilities：Tool、MCP、Skill、Plugin

### 目标

解释 Agent 当前能做什么、哪些能力被禁用、哪些能力有副作用风险。

### 页面分区

1. Tool Registry
2. MCP Servers
3. Tool Calls
4. SideEffectReceipts
5. Reconcile Queue
6. Skills
7. Plugins
8. Sandbox Profiles

### Tool 字段

- capability_id
- name
- namespace
- toolset
- risk_level
- side_effect_type
- input_schema hash
- health
- enabled
- source
- plugin_id

### MCP 字段

- server name
- transport
- enabled
- toolset
- allowed_tools
- trust_label
- max_output_chars
- health
- last_error

### Receipt 字段

- receipt_id
- capability_id
- attempt_type
- attempt_id
- external_operation_id
- request_hash
- status
- reconcile_status
- raw_ref
- created_at
- resolved_at

### 功能

- disable tool；
- disable plugin；
- restart MCP server；
- inspect schema diff；
- reconcile receipt；
- approve risky tool；
- view sandbox profile；
- archive/restore/pin skill；
- view plugin lifecycle state。

### 必须补齐的 API

- `GET /api/capabilities`
- `GET /api/tool-calls`
- `GET /api/receipts`
- `GET /api/reconcile`
- `GET /api/skills`
- `POST /api/commands/disable-tool`
- `POST /api/commands/reconcile-receipt`
- `POST /api/commands/archive-skill`
- `POST /api/commands/restore-skill`

## 6.10 Trace & Audit：因果链与审计

### 目标

解释系统行为，而不是只显示日志。

### Trace 展示

Trace Tree 应展示：

1. root trace；
2. correlation_id；
3. causation_id；
4. inbound message；
5. command；
6. event；
7. task；
8. model call；
9. tool call；
10. delivery attempt；
11. audit record；
12. payload refs。

### Audit 展示

必须展示以下操作：

- approval respond；
- command execute；
- memory confirm/delete；
- connector pause/resume；
- plugin disable；
- delivery replay；
- config change；
- proactive policy update；
- backup restore；
- payload export；
- replay event。

### 功能

- search by trace_id；
- search by correlation_id；
- search by entity ID；
- filter by event type；
- expand span；
- view redacted payload；
- request debug payload with explicit permission；
- export trace bundle。

### 安全

- 默认 Trace 只显示脱敏摘要；
- Secret 永不显示明文；
- PII 默认脱敏；
- Payload 权限视图必须显示 `sensitivity`、`retention_class`、`storage_uri`、`hash`，不直接打开敏感内容。

## 6.11 System：配置、存储、备份与健康

### 目标

让 Owner 能判断系统是否可持续运行，是否需要备份、恢复、清理或降级。

### 页面分区

1. Health
2. Config
3. Profile
4. SQLite
5. Payload Store
6. Backup / Restore
7. Resource Budget
8. Degradation
9. Runbook

### Health 指标

- liveness
- readiness
- sqlite status
- payload status
- provider status
- gateway status
- worker status
- scheduler status
- connector freshness
- delivery backlog
- outbox backlog

### Config 指标

- schema_version
- config_version
- content_hash
- source_layers
- active profile
- secret refs
- hot reload status
- last failed reload

### Storage 指标

- db_path
- db_size
- wal_size
- payload_dir
- payload_size
- object_count
- orphan_count
- backup_count
- latest_backup_at
- latest_restore_drill_at

### 功能

- config dry-run；
- rollback config；
- create backup；
- verify backup；
- restore to recovery profile；
- payload GC dry-run；
- rebuild indexes；
- view runbook scenario；
- export diagnostic bundle。

危险操作必须：

1. 显示影响范围；
2. 要求二次确认；
3. 走 Command API；
4. 写 Audit；
5. 提供结果和回滚建议。

## 7. 指标模型

### 7.1 Hero Metrics

| 指标 | 口径 | 数据源 | 默认窗口 |
|---|---|---|---|
| Readiness | API、DB、Worker、Gateway、Recovery 均无阻断 | health/status projection | 当前 |
| Attention Count | pending approval + unknown delivery + failed task + dead letter + memory candidate | 聚合查询 | 当前 |
| Turn Success Rate | completed turns / finished turns | turns | 24h |
| Median Turn Latency | completed_at - created_at | turns/run_attempts/model_calls | 24h |
| Delivery Success Rate | sent / attempted | deliveries/delivery_attempts | 24h |
| Proactive Live Risk | live + high budget usage + failures + quiet hour override | proactive_decisions/policy | 当前 |
| Model Cost Proxy | input + output tokens，按 provider/model 汇总 | model_calls | 24h |
| Backlog Age | queued Task/Delivery/Outbox 最老等待时间 | tasks/deliveries/outbox_events | 当前 |

### 7.2 诊断指标

| 类别 | 指标 |
|---|---|
| Runtime | turns by status、attempts by status、lease expired、waiting_user、checkpoint count |
| Model | calls、tokens、latency、error category、retry count、finish_reason |
| Task | queued/running/waiting/failed、retry scheduled、dead letter、misfire count |
| Delivery | pending/sending/sent/unknown/failed、attempt count、receipt kind、reconcile backlog |
| Connector | freshness、items ingested、dedupe rate、cursor age、auth failure、quarantine count |
| Proactive | candidates、decisions by action、dry_run count、budget usage、quiet hour deferrals |
| Memory | confirmed/candidate/rejected、retrieval hits、conflicts、stale/expired |
| Tool | calls、denied、unknown、receipt reconcile、high risk usage |
| Storage | sqlite busy、db size、payload size、backup freshness、orphan payload |
| Security | approval pending、policy denied、sandbox violation、secret redaction events |

### 7.3 状态颜色规则

- `ok`：completed、succeeded、sent、published、confirmed、healthy、active；
- `info`：queued、scheduled、dry_run、candidate、waiting；
- `warn`：retry、unknown、degraded、paused、stale、near_budget；
- `danger`：failed、dead_letter、auth_error、policy_violation、corruption、secret_leak_suspected；
- `muted`：archived、deleted、disabled、discarded。

## 8. 数据源与 API 设计

### 8.1 原则

1. API 返回面向 Dashboard 的 DTO，不直接暴露内部表结构。
2. 每个 DTO 包含 `schema_version`、`generated_at`、`source_freshness`。
3. 列表默认分页，最大 limit 受限。
4. 详情页按需加载大对象。
5. Payload 内容默认不内联，只返回 ref 和安全摘要。
6. 所有写操作返回 `command_id`、`status`、`target_id`、`previous_version`、`new_version`、`audit_id`。

### 8.2 新增 Query API

优先新增：

- `GET /api/dashboard/summary`
- `GET /api/dashboard/attention`
- `GET /api/health/components`
- `GET /api/proactive/status`
- `GET /api/proactive/candidates`
- `GET /api/proactive/decisions`
- `GET /api/proactive/scheduled-requests`
- `GET /api/proactive/digests`
- `GET /api/outbox`
- `GET /api/events`
- `GET /api/audit`
- `GET /api/capabilities`
- `GET /api/tool-calls`
- `GET /api/receipts`
- `GET /api/config/versions`
- `GET /api/storage/summary`
- `GET /api/backups`

### 8.3 新增 Command API

优先新增：

- `POST /api/commands/update-proactive-policy`
- `POST /api/commands/review-proactive-candidate`
- `POST /api/commands/import-proactive-context`
- `POST /api/commands/force-connector-poll`
- `POST /api/commands/replay-event`
- `POST /api/commands/reconcile-delivery`
- `POST /api/commands/reconcile-receipt`
- `POST /api/commands/create-backup`
- `POST /api/commands/verify-backup`
- `POST /api/commands/restore-backup`
- `POST /api/commands/config-dry-run`
- `POST /api/commands/rollback-config`
- `POST /api/commands/payload-gc-dry-run`

### 8.4 Stream 事件

建议事件类型：

- `turn.updated`
- `run_attempt.updated`
- `task.updated`
- `schedule.fired`
- `outbox.updated`
- `connector.updated`
- `connector.item_ingested`
- `proactive.candidate_created`
- `proactive.decision_made`
- `delivery.updated`
- `approval.updated`
- `memory.updated`
- `tool_call.updated`
- `receipt.updated`
- `audit.created`
- `health.changed`
- `config.changed`

每个事件包含：

- schema_version
- event_id
- event_type
- entity_type
- entity_id
- status
- previous_status
- occurred_at
- trace_context
- safe_summary

## 9. 前端设计规范

### 9.1 布局

使用三层布局：

1. 左侧导航：一级页面；
2. 顶部状态栏：全局运行状态和数据模式；
3. 主内容：summary cards → charts → tables → details。

### 9.2 页面密度

默认页面应做到：

- 第一屏看到关键状态和待处理事项；
- 诊断详情放在折叠区；
- 表格默认 50 行，支持分页；
- 长 JSON 默认折叠并脱敏；
- 所有对象 ID 可复制；
- 所有关联对象可点击跳转。

### 9.3 视觉规则

- 状态颜色统一；
- 风险动作按钮使用 danger；
- dry-run 与 live 用明显不同样式；
- mock/demo 数据有全局横幅；
- 时间统一显示相对时间 + tooltip 精确时间；
- 金额/Token/延迟带单位。

### 9.4 空状态

空状态必须说明：

1. 当前没有数据；
2. 是系统健康无待办，还是数据源尚未接入；
3. 下一步可执行动作。

示例：

- “暂无 pending approval：当前无需人工审批。”
- “暂无 proactive candidates：主动系统 disabled 或无新来源。”
- “暂无 Connector items：请检查 Connector 是否 enabled 或最近 poll 是否成功。”

## 10. 安全与权限

### 10.1 本地控制面

本阶段只支持本地控制面：

- 默认监听 loopback；
- 远程访问必须先补认证和 TLS；
- 写操作校验 Origin；
- Command 记录 Owner Principal、source、idempotency_key。

### 10.2 Secret 与 PII

Dashboard 不显示：

- API key
- token
- cookie
- password
- raw personal contact identifier
- 未脱敏 payload
- hidden chain-of-thought

Dashboard 可以显示：

- secret_ref
- masked value
- sensitivity label
- payload hash
- safe summary

### 10.3 危险操作确认

以下操作必须二次确认：

- 切换 proactive live；
- replay delivery；
- replay event with side effects；
- restore backup；
- delete profile；
- payload GC execute；
- disable plugin/tool；
- rollback config；
- export payload bundle。

## 11. 当前代码差距

### 11.1 必须修正

1. 前端 mock fallback 需要改为显式 demo mode。
   - 当前 `web/src/api.ts` 请求失败会自动 fallback。
   - 真实运行模式必须失败即报错，避免误导。

2. 新增 Proactive 页面。
   - 当前没有专门主动系统页面。
   - 这是产品闭环最关键的页面。

3. 新增 Health/System 页面。
   - 当前 `/api/status` 只提供基础计数。
   - 需要组件级 health、storage、backup、config、resource budget。

4. Query API 需要增加 dashboard summary。
   - 当前 Overview 需要分别拉 `/status` 和 `/usage`。
   - 应由 `/dashboard/summary` 聚合当前一屏所需数据。

5. Attention Inbox 缺失。
   - 当前没有统一待处理事项 API。
   - Owner 无法一眼看到 pending approval、failed task、unknown delivery、memory candidate。

### 11.2 应补齐

1. Outbox/Event/DeadLetter 页面；
2. Delivery Attempt/Receipt/Reconcile 详情；
3. ToolCall/Receipt/Reconcile 页面；
4. Config Version/Hot Reload 页面；
5. Backup/Restore 页面；
6. Payload 权限视图；
7. ProactivePolicy Import Command 页面；
8. Audit 搜索页面。

### 11.3 可延后

1. 多用户权限系统；
2. 远程访问；
3. BI 风格复杂图表；
4. 自定义 Dashboard builder；
5. 移动端完整适配；
6. 外部遥测系统集成。

## 12. 实施计划

### Phase D1：Dashboard 安全基线

目标：避免 Dashboard 误导用户。

工作项：

1. `web/src/api.ts` 增加模式：
   - `real`
   - `demo`
   - `mock_fallback`
2. 生产默认禁止自动 fallback；
3. 顶栏显示数据模式；
4. mock mode 禁用 Command；
5. 所有错误显示真实 API error。

验收：

- 后端断开时真实模式显示错误，不展示假数据；
- `VITE_MOCK=1` 时明确显示演示模式；
- Command 在 mock mode 下不可执行。

### Phase D2：Summary 与 Attention API

目标：让 Overview 成为真正的操作首页。

新增 API：

- `GET /api/dashboard/summary`
- `GET /api/dashboard/attention`
- `GET /api/health/components`

工作项：

1. 聚合 status、usage、backlog、recovery、health；
2. 生成 attention items；
3. 每个 item 提供 target route 和 recommended action；
4. 增加 freshness metadata。

验收：

- Overview 第一屏能看到健康、待办、主动系统、资源；
- 每个待办都能跳转到详情；
- 无待办时显示明确空状态。

### Phase D3：Proactive 页面

目标：补齐主动系统产品闭环。

新增 API：

- `GET /api/proactive/status`
- `GET /api/proactive/candidates`
- `GET /api/proactive/decisions`
- `GET /api/proactive/scheduled-requests`
- `GET /api/proactive/digests`
- `POST /api/commands/review-proactive-candidate`
- `POST /api/commands/update-proactive-policy`

工作项：

1. Candidate Queue；
2. Decision Log；
3. Dry-run Review；
4. Scheduled Requests；
5. Digest Buckets；
6. Feedback summary；
7. Policy controls。

验收：

- 能解释每个 candidate 的来源和决策原因；
- dry-run 下能看到“本应发送”；
- live 切换必须二次确认并审计；
- 操作后状态可通过 Query API 重同步。

### Phase D4：Delivery / Trace / Audit 收敛

目标：解释副作用。

工作项：

1. Delivery 详情页展示 Attempt 和 Receipt；
2. Unknown/Reconcile 队列；
3. Trace Tree 展示 Command/Event/Task/Delivery；
4. Audit 列表和详情；
5. Payload 权限视图。

验收：

- 能从任一 Delivery 跳到 Message、Turn、Trace；
- unknown delivery 有明确 reconcile 操作；
- Audit 可按 entity_id 查询；
- Payload 不泄露敏感内容。

### Phase D5：Capabilities 页面

目标：治理 Tool/MCP/Skill/Plugin。

工作项：

1. Capability Registry 列表；
2. MCP server health；
3. ToolCall 列表；
4. SideEffectReceipt 列表；
5. Skill 生命周期；
6. Plugin 生命周期；
7. Sandbox profile 展示。

验收：

- 能看到每个能力的来源、风险、副作用类型；
- 高风险工具调用可追踪 receipt；
- Plugin/Tool disable 走 Command API。

### Phase D6：System 页面

目标：本地运行可运维。

工作项：

1. Health components；
2. Config version；
3. Backup/Restore；
4. Payload Store；
5. SQLite/WAL；
6. ResourceBudget；
7. Degradation；
8. Runbook。

验收：

- 可看到 backup freshness；
- 可执行 backup verify；
- restore 默认进入 recovery profile；
- secret 只显示 ref；
- payload GC 默认 dry-run。

## 13. 非功能要求

### 13.1 性能

- Overview 初次加载目标 < 800ms；
- 大表分页；
- 详情按需加载；
- Trace 大对象懒加载；
- Payload 不默认加载；
- charts 数据预聚合。

### 13.2 可维护性

- 前端 API types 与后端 DTO 对齐；
- 每个页面只依赖 Query/Command API；
- 不在前端拼接业务状态机；
- 状态颜色和字段格式集中定义；
- mock 数据必须与真实 DTO 共用 schema。

### 13.3 可解释性

所有关键对象必须有：

- status；
- reason；
- trace_id/correlation_id；
- source；
- created_at/updated_at；
- version 或 attempt_no；
- recommended_action；
- safe_error。

## 14. 完成定义

Dashboard 设计完成并可进入实现时，应满足：

1. Overview 能回答系统是否健康、是否需要处理、主动系统是否安全；
2. Proactive 页面能解释 candidate、decision、dry-run、send_later、digest 和 feedback；
3. 所有写操作走 Command API；
4. 所有只读数据走 Query API；
5. mock/demo 与 real 明确区分；
6. Trace/Audit 能串起 Command、Event、Task、Delivery；
7. Secret 和敏感 Payload 不泄露；
8. 系统资源、备份、配置和降级状态可见；
9. 每个页面有空状态、错误状态、刷新状态；
10. 断线后能通过 Query API 重同步。

## 15. 建议 PR 拆分

1. PR-D1：移除隐式 mock fallback，增加 real/demo 数据模式。
2. PR-D2：新增 `/api/dashboard/summary`、`/api/dashboard/attention`、`/api/health/components`。
3. PR-D3：重构 Overview 为操作首页。
4. PR-D4：新增 Proactive 页面和 API。
5. PR-D5：增强 Delivery 详情、Attempt、Receipt、Reconcile。
6. PR-D6：增强 Trace & Audit。
7. PR-D7：新增 Capabilities 页面。
8. PR-D8：新增 System 页面。
9. PR-D9：接入 Stream API，完成断线重同步。
10. PR-D10：统一 DTO、空状态、错误状态、可访问性和安全审查。

