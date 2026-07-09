# 04 后台任务、Scheduler、Event、Connector 与主动系统下一步开发计划

> 状态：draft  
> 对应目录：`markdown/04_background`  
> 设计依据：`PROACTIVE-TASKS`、`TASK-SCHEDULER`、`EVENT-OUTBOX`、`CONNECTOR-INGESTION`、`PROACTIVE-IDLE`

## 1. 目标

把当前 Task/Scheduler/Outbox/RSS 与正在开发的 MCP Connector、主动候选、决策和 Digest 串成一套可恢复、可重放、零隐藏副作用的后台执行系统。

完成后应满足：

1. Task 每次执行都有 TaskAttempt、Lease、Checkpoint、预算和明确 Outcome；
2. Schedule 触发使用稳定 fire key，重启和 Misfire 不重复；
3. Event 事务发布、Aggregate 内顺序、Consumer Inbox 和 Replay 完整闭环；
4. Connector 先归档、再标准化、再推进 Cursor，可从 RawItem 重放；
5. alert/content/context 三路具有不同延迟与约束；
6. ProactivePolicy 是权威事实，Markdown 只是可重建视图；
7. 主动决策、Digest、延迟发送、反馈和 Drift 均可解释、可审计、可 dry-run。

设计引用：

- `PROACTIVE-TASKS / 1. Data & Event`
- `PROACTIVE-TASKS / 3. Proactive Decision`
- `TASK-SCHEDULER / 3. 状态机`
- `TASK-SCHEDULER / 7. Schedule`
- `EVENT-OUTBOX / 2. 发布事务`
- `CONNECTOR-INGESTION / 3. Poll 协议`
- `PROACTIVE-IDLE / 5. 决策顺序`
- `PROACTIVE-IDLE / 9. 空闲判定`

## 2. 当前基线

### 已有

- Task/TaskAttempt 领域对象、Repository、Dispatcher、Worker、Handler Registry；
- Lease、版本检查、Retry、Recovery 基础；
- Schedule、ScheduledFire、Duration/every/cron 基础解析；
- Transactional Outbox 和 Worker；
- RSS Connector、Cursor、Raw/Normalized Item、Digest；
- MCP Connector、Event Consumer、Candidate、Energy、Policy、Delivery/Digest 的大量 WIP；
- Migration 已扩展到 0028；
- Connector、Scheduler、Proactive、MCP 测试基础。

### 当前阻断

- WIP 存在 Ruff 错误和未定义 `asyncio_run_safe`；
- MCP 测试硬编码不存在的 Python 解释器；
- 环境缺少 feedparser 导致 RSS 测试失败；
- Event Consumer、主动决策和投递代码尚未达到发布门禁；
- 部分功能测试覆盖了 happy path，但未覆盖崩溃窗口、重复消费和跨午夜边界。

## 3. 边界和对象职责

```text
Connector       只负责外部事实进入系统
Event           表示已经发生的不可变事实
Task            跨重启执行的逻辑工作
Schedule        只产生到期触发，不执行副作用
Candidate       待主动决策的派生对象
Decision        固化规则输入、结果和原因
ScheduledRequest 尚未到发送时机，不固定 Endpoint
Delivery        ready-to-send 后固定 TargetSnapshot
Drift           可撤销的低优先级 Task
```

任何实现不得把上述对象合并成一个通用 Job 表或隐藏 Pipeline。

## 4. 里程碑

### M0：稳定当前 WIP

1. 修复所有 F821、未使用导入、循环导入和格式问题；
2. 删除或实现占位函数，禁止未执行分支藏未定义名称；
3. MCP Fixture 使用 `sys.executable` 或测试环境解释器；
4. 项目以 editable/dev 依赖安装，RSS 测试不依赖全局环境；
5. 执行 Migration 空库、重复启动和 0024→0028 升级测试；
6. 将当前主动功能默认设为 dry_run/disabled，直到 M5 通过；
7. Python 测试、Ruff、compileall 全绿后再扩展功能。

### M1：TaskAttempt、Lease 与 Outcome 收敛

#### 工作项

1. 每次进入 running 原子创建新 TaskAttempt；
2. 唯一约束 `(task_id, attempt_no)`；
3. 领取条件包含状态、scheduled_at/next_attempt_at、Lease 到期；
4. 提交结果必须匹配 owner/version/current attempt；
5. Heartbeat 在 Lease 1/3 周期内更新，失败后 Handler 停止开始新副作用；
6. Handler 统一返回 Complete、Retry、WaitUser、WaitExternal、Spawn、Cancel、Fail；
7. waiting 状态不持有 Lease；
8. 长步骤拆分为有界步骤，完成后写 Checkpoint 并重新排队；
9. Attempt 次数、预算和资源使用跨重启累计；
10. unknown Tool/Delivery 不进入普通 Retry。

#### 测试

- 双 Worker 同时领取；
- Lease 过期和旧 Worker 返回；
- Heartbeat 失败；
- Worker 在结果提交前崩溃；
- waiting_user 重启恢复；
- Retry 上限和 jitter；
- 取消与完成竞态。

### M2：Scheduler 和 Misfire

#### 工作项

1. Schedule 与 Task 严格分离；
2. 统一解析顺序：ISO → every → Duration → 5-field cron；
3. 保留原始 expression 和规范化结果；
4. 触发幂等键使用 `schedule_id + scheduled_fire_time`；
5. 实现 skip、run_once、catch_up_limited、merge；
6. once 固定 run_once + grace window；
7. Schedule timezone 显式存储；
8. DST 重复/缺失时间采用确定策略；
9. Scheduler 只写 ScheduledFire/Task，不直接调用 Handler；
10. priority + scheduled_at + aging，避免低优先级永久饥饿。

#### 测试矩阵

- 30s/365d/零值/非法 Duration；
- every monday/day/间隔；
- 跨 DST spring forward/fall back；
- 重启重复 tick；
- 一次错过、多次错过、merge payload；
- 并行 Scheduler 竞争同一 Fire。

### M3：Event/Outbox/Inbox 可靠性

#### 工作项

1. 业务状态、aggregate version 和 Outbox 在同一事务；
2. Event 使用过去式命名，不承载 Command；
3. Outbox 状态统一 pending/leased/published/retry/dead_letter；
4. 同 Aggregate 仅领取当前最小未发布版本；
5. Consumer 唯一键 `(consumer_name,event_id)`；
6. Consumer 自身派生状态、后续 Outbox 和 Inbox succeeded 同事务；
7. 一个 Consumer 失败不阻塞其他 Consumer；
8. Schema 不支持或永久错误进入 dead letter；
9. Consumer 声明版本范围，Upcaster 记录转换版本；
10. Replay Command 指定范围、Consumer、dry-run 和副作用禁用。

#### 验收

- 业务事务回滚时无 Event；
- 发布成功、本地提交前崩溃允许重复但 Consumer 幂等；
- Aggregate v2 不越过 pending v1；
- 版本缺口不猜测；
- Replay 不写生产 Inbox、不创建真实 Delivery。

### M4：Connector 摄取协议

#### 通用管道

```text
Poll Task
→ connector lease
→ cursor/conditional token
→ bounded fetch
→ immutable Raw Payload
→ normalize
→ dedup/version/retract
→ commit item + source event + cursor + outbox
→ acknowledge after commit
```

#### 工作项

1. 统一 RSS/MCP/后续 Webhook 的 ConnectorInstance/Batch/RawItem/NormalizedItem；
2. 所有数据固定 external_untrusted；
3. Cursor 带版本、失败计数、next_poll_at 和 Lease；
4. Cursor 只在已接受项安全持久化后推进；
5. 大批次按来源稳定顺序分段；
6. 去重优先稳定外部 ID，再版本，再内容 hash；
7. Updated/Retracted 创建新 Event，不修改历史含义；
8. 单个坏 Item 进入 quarantine，并审计跳过和推进原因；
9. 认证失败暂停 Connector 并通知，不持续重试；
10. acknowledge 在 Commit 后执行，失败可幂等重试；
11. Normalize 版本升级从 RawItem 重放，默认不触发主动副作用；
12. Webhook 先验签、限大小、写 Inbox/Raw，Task 中 Normalize。

#### Connector 契约测试

- 重复页、Cursor 回退；
- 条件请求/304；
- Item 更新和撤回；
- 坏 Item；
- Retry-After；
- Commit 前后崩溃；
- Webhook 重放；
- Normalize v1→v2；
- MCP 分页、Schema 变化、Server 断连。

### M5：三路 Candidate 投影

#### 工作项

1. Connector 注册默认 stream_type，配置允许按来源覆盖；
2. Candidate 幂等键由 source facts + candidate type 生成；
3. alert：快速通道、<30s、每小时上限、超限降级 content；
4. content：完整 novelty/relevance/quiet/budget 路径；
5. context：不直接推送，只作为 External Untrusted Context 或 idle fallback；
6. Candidate 保存 source ids、principal、topic、score、expiry、policy version；
7. Event Consumer 仅产生 Candidate/Task/Command，不直接发送；
8. 重复 Event 不产生重复 Candidate；
9. 过期 Candidate 不能发送；紧急升级由新 Event 表达。

### M6：ProactivePolicy 和派生视图

#### 工作项

1. SQLite ProactivePolicy 作为唯一权威；
2. UpdateProactivePolicy Command 使用 expected_version；
3. 更新聚合时写 Audit 和 Outbox；
4. 投影 Task 原子渲染 PROACTIVE_CONTEXT.md；
5. 文件缺失/损坏从数据库重建；
6. 人工编辑只能经 Import Command：解析、校验、展示 Diff、版本检查；
7. Proactive Agent 读取版本化聚合，Markdown 只供人工查看；
8. 与 Preference 冲突时保存选择理由和被覆盖版本；
9. Policy 变更不追溯修改历史 Decision。

### M7：确定性主动决策

#### 决策顺序

严格实现 `PROACTIVE-IDLE / 5`：alert fast path → safety/source → duplicate/novelty → relevance → energy → urgency/expiry → quiet/cooldown → budget → optional model → deterministic aggregation。

#### 工作项

1. 每个 Gate 产生结构化结果和 reason code；
2. LLM score 不能覆盖 hard deny、Quiet Hours 或预算；
3. 能量公式与三时间尺度参数版本化；
4. 能量只调整 tick/urgency/novelty，不直接决定发送；
5. alert 不受 Quiet Hours/cooldown/daily budget，但受 hard safety 和独立每小时上限；
6. content 完整受控；context 不直接发送；
7. 输出 send_now/send_later/digest/silent/create_task/ask_permission/discard；
8. Decision 保存输入摘要、Policy version、Gate trace、模型版本、预算和最终理由；
9. dry_run 执行完整判断但不创建真实 Delivery；
10. dry-run 结果可在 Dashboard 人工评价。

#### 数值/边界测试

- 无历史、刚互动、1h、8h、48h；
- Quiet Hours 跨午夜；
- topic/hour/day/channel 预算边界；
- 同 Event、同 Topic 冷却；
- alert 降级 content；
- policy 更新与评估并发；
- LLM 建议违反 hard rule。

### M8：发送、ScheduledRequest 和 Digest

#### 工作项

1. send_now 通过 DeliveryService 创建 Delivery；
2. send_later 先创建 ScheduledDeliveryRequest，不提前固定 Endpoint；
3. 到期时重新执行 DeliveryPolicy，再创建 TargetSnapshot；
4. Preference/Endpoint 在等待期变化时以发送时策略为准；
5. Digest 按 Principal/date/topic 分桶；
6. deterministic selection、排序、去重后再摘要；
7. 同 Candidate 消费后不能即时重复发送；
8. content 默认最大延迟 6h；alert 不进入 Digest；
9. 投递成功后更新实际发送时间，用于 cooldown；
10. 失败交给 Delivery 生命周期，不重新执行主动决策。

### M9：反馈和 Drift

#### 反馈

- opened/ignored/dismissed/useful/not_useful/muted/requested_more 写 FeedbackEvent；
- 反馈生成 Preference Candidate，不直接永久调权；
- 策略变化版本化并可回滚。

#### Drift

1. Resource Manager 同时检查高优先级 backlog、保留并发、存储健康、恢复状态和日预算；
2. Drift 只创建正常 Task/Checkpoint；
3. 允许 Memory 去重建议、Embedding、索引、摘要、GC 扫描和视图检查；
4. 默认禁止发送、外部修改、确认 Memory、删除数据、安装 Plugin；
5. 新用户 Turn 到达时停止领取新步骤；
6. 当前步骤在安全点保存 Checkpoint 并释放资源；
7. Drift 不启动隐藏无限线程。

## 5. 配置与数据迁移

配置按功能逐项启用：

- background.task pools/lease/retry；
- schedules timezone/misfire；
- connectors batch/concurrency/retention；
- proactive streams/energy/quiet/budgets/dry_run；
- drift budget/allowed task types。

新表先以 dry-run 写入验证，再启用真实投递。0025～0028 必须补上一版本升级和中断恢复测试。

## 6. 观测指标

- Task queue age、Lease lost、Retry、Attempt duration；
- Schedule Misfire、catch-up、duplicate fire；
- Outbox backlog/age、version gap、dead letter；
- Connector latency、Cursor lag、quarantine、dedup；
- alert/content/context 比例和延迟；
- Candidate→Decision→Delivery 转化；
- Energy 分布、Quiet Hours 延迟、预算拒绝；
- Digest 命中、反馈、Drift 时长和抢占延迟。

## 7. 完成定义

1. 当前 WIP 全部通过测试、Ruff、compileall；
2. Task/Schedule/Event/Connector 的崩溃和重复路径可验证；
3. Cursor 不会越过未安全持久化数据；
4. Replay/dry-run 不产生真实副作用；
5. 主动决策每个 Gate 可解释且模型不能绕过硬规则；
6. ScheduledRequest 和 Delivery 边界清晰；
7. `TASK-SCHEDULER / 11`、`EVENT-OUTBOX / 10`、`CONNECTOR-INGESTION / 11`、`PROACTIVE-IDLE / 13` 的测试矩阵全部落地。

## 8. 建议拆分 PR

1. PR-B0：WIP 修复与可重复测试环境；
2. PR-B1：TaskAttempt/Lease/Checkpoint/Outcome；
3. PR-B2：Scheduler/Misfire/DST；
4. PR-B3：Event 顺序、Inbox、Replay；
5. PR-B4：Connector 通用管道、Quarantine、回放；
6. PR-B5：Candidate 三路投影与 Policy 视图；
7. PR-B6：Decision Engine、Energy、dry-run；
8. PR-B7：ScheduledRequest/Digest/Delivery；
9. PR-B8：Feedback 与 Drift 抢占。
