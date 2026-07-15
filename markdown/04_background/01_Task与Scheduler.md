---
doc_id: "TASK-SCHEDULER"
title: "Task 与 Scheduler"
version: "1.2"
status: "active"
source_of_truth: true
layer: "functional-spec"
domain: "background"
authority: "task-scheduler"
scope: "Task、TaskAttempt、Lease、Schedule 表达式、等待、重试和恢复"
tags: ["task", "scheduler", "lease", "schedule-expression"]
depends_on: ["RUNTIME-FLOWS", "APPROVAL-COMMANDS"]
related_docs: ["EVENT-OUTBOX", "PROACTIVE-IDLE", "DATABASE-SCHEMA"]
language: "zh-CN"
---

# Task 与 Scheduler

## 1. 对象

```text
Task
├── TaskAttempt
├── Checkpoint
├── WaitingCondition
└── ChildTaskLink

Schedule
└── ScheduledFire → Task
```

Task 是逻辑工作；TaskAttempt 是 Worker 的一次执行占用。

## 2. Task 字段

```text
task_id/type
payload_ref
status
priority
scheduled_at
next_attempt_at
retry_policy
idempotency_key
checkpoint_id
waiting_condition_ref
parent_task_id
budget
version
created_at/completed_at
```

### 2.1 TaskAttempt

```text
task_attempt_id
task_id
attempt_no
status: created | running | succeeded | failed | cancelled | abandoned
lease_owner
lease_version
started_at/finished_at
checkpoint_id
error_ref
resource_usage
trace_id
```

唯一约束 `(task_id, attempt_no)`。TaskAttempt 只表示一次 Worker 执行权；重试、等待后恢复和 Lease 丢失后恢复都创建新 Attempt。旧 Attempt 进入 `abandoned` 后不得提交 Task 状态或新副作用。

## 3. 状态机

```text
created → queued/scheduled → running → completed
                              ├→ queued(retry)
                              ├→ waiting_user
                              ├→ waiting_external
                              └→ failed/cancelled/expired
```

每次进入 `running` 创建新 TaskAttempt。当前 Attempt `succeeded` 可以表示有界步骤安全完成并把 Task 转入 waiting 状态，不等于整个 Task completed。等待态不持有 Lease。

## 4. Lease

领取事务使用条件更新：状态可运行、时间到期、无有效 Lease。写入 `lease_owner`、`lease_version`、`lease_expires_at` 和 Attempt。提交结果必须匹配 Lease owner/version；旧 Worker 结果仅保存诊断。

Heartbeat 在 Lease 的三分之一周期内更新。无法续租时 Handler 应尽快停止，不继续开始新副作用。

## 5. Handler 协议

```text
Complete(result)
Retry(error,next_at)
WaitUser(approval)
WaitExternal(condition,next_check_at)
Spawn(children,join_policy)
Cancel(reason)
Fail(error)
```

一次调用执行有界步骤，长期循环必须写 Checkpoint 并重新排队。

Agent Tool 创建/取消 Schedule 不直接写 Repository，而是发送
`CreateAgentSchedule` / `CancelAgentSchedule` Command。Handler 在同一事务写
聚合、Command Audit 和 Event Outbox，幂等键为 `tool_call_id + action_hash`。

## 6. Retry

RetryPolicy 定义次数、错误码、指数退避、上限和 jitter。Attempt 数和预算跨重启累计。副作用 unknown 不进入普通 Retry，先对账。

`vision.analyze` 在创建 Task 时写入固定
`retry_policy = {max_attempts: 3, backoff_seconds: [5, 30, 120]}`
（`multimodal_repo.enqueue_analysis_task`）。Provider 明确声明 retryable
时，当前 Attempt 结束为 failed，Task 按 backoff 重新排队，领取时创建新
Attempt，沿用同一 `analysis_id` 与 Cache Key；非 retryable 错误直接终止。

## 7. Schedule

### 7.1 Schedule 对象

```text
schedule_id
type: once|interval|cron|calendar|condition
expression
timezone
misfire_policy: skip|run_once|catch_up_limited|merge
max_catch_up
enabled
next_fire_at
version
```

触发幂等键为 `schedule_id + scheduled_fire_time`。夏令时重复/缺失时间按 Schedule timezone 和显式策略处理。

### 7.2 调度表达式

除标准 5-field cron 表达式外，调度器还支持更易读的自然语言格式。三种表达式共存在 `expression` 字段中，解析时按优先级判断类型：

```text
解析顺序:

1. ISO 时间戳     "2026-07-15T09:00:00Z"
      → type=once, fire_at=解析时间

2. "every" 短语   "every 2h" / "every 30m" / "every monday 9am"
                    "every day 08:00" / "every 1d"
      → type=interval, 内部转换为 Duration 或 cron

3. Duration 格式  "30s" / "5m" / "2h" / "1d" / "1h30m"
      → type=interval, period=解析后的 timedelta

4. 5-field cron    "0 9 * * 1-5"  （标准 cron）
      → type=cron
```

**Duration 格式规则**：

```text
格式: (Nd)?(Nh)?(Nm)?(Ns)?
示例:
  "30s"   → 30 秒
  "5m"    → 5 分钟
  "2h"    → 2 小时
  "1d"    → 1 天
  "1h30m" → 1 小时 30 分钟
  "1d6h"  → 1 天 6 小时

要求: 至少一个非零单元
限制: 最小 30s，最大 365d
```

**"every" 短语规则**：

```text
格式: "every" <间隔> | "every" <星期> <时间> | "every day" <时间>

示例:
  "every 2h"         → 每 2 小时
  "every 30m"        → 每 30 分钟
  "every monday 9am" → 每周一 09:00
  "every day 08:00"  → 每天 08:00
  "every 1d"         → 每天一次（从首次触发时间起算）

星期支持: monday/tuesday/.../sunday（英文全名）
          mon/tue/wed/thu/fri/sat/sun（英文缩写）
时间格式: "9am" / "9:00" / "09:00" / "9:00am" / "21:00"
```

Duration 和 "every" 短语最终都会转换为 interval 或 cron 类型的 Schedule。原始表达式保留在 `expression` 字段中，供用户编辑和回显。

### 7.3 Misfire 策略

```text
skip              错过就跳过，不补跑
run_once          立即补跑一次（不管错过了多少次）
catch_up_limited  补跑最近的 N 次（由 max_catch_up 限制，默认 3）
merge             所有错过的触发合并为一次执行，payload 中包含完整的 fire_time 列表
```

默认为 `catch_up_limited`，max_catch_up=3。one-shot 类型的 Schedule 始终使用 `run_once`（grace window 120s）。

## 8. 等待与子任务

WaitingCondition 保存类型、目标、过期时间、轮询时间和恢复 Checkpoint。子任务声明 `join_policy: all|any|none`、失败策略和最大深度；创建子任务与父状态更新同事务。

`delegate_task` 将旧单 `prompt` 规范化为一个 `tasks[]` 元素；一次最多三个子
Agent、同一父 Attempt 并发最多两个、深度最多二。权限是父 CapabilitySnapshot、
请求 Toolset 与本地子 Agent Policy 的交集，排除 Channel/Delivery/权限管理。
父 Attempt 结束后 Turn=`waiting_external`；子 Task 使用独立 Turn、RunAttempt 和
预算执行。每个 Child 的 `usage_json` 在 Join Evaluator 中聚合到父
`agent_delegations.usage_json`，供状态查询和后续预算治理使用。Join `all|any` 满足后
父 Turn 入队，新 Attempt 从 Checkpoint 恢复。

`tasks[]` 支持 `role=general|researcher|coder|reviewer|planner`。角色预设由本地
确定性策略定义系统指令、可用 Toolset 和预算上限；`researcher`、`reviewer`、
`planner` 仅能获得 `side_effect_class=none` 的只读 Capability。调用方可通过
`budget` 请求更小的模型、Tool、Token、墙钟和成本预算，Runtime 取角色上限、
请求值、父 Attempt 剩余预算三者交集，并按 Child 数量分配父剩余预算。角色、
实际 Toolset 和实际预算随 Child Task payload 固化，等待或崩溃恢复不得扩大。

Join 结果固定包含 `delegation_id/status/join_policy/failure_policy/usage/children`；
每个 Child 返回 `client_id/task_id/turn_id/role/toolsets/budget/status/result_summary/
result_ref/usage/error`。结果正文可进入 Payload Store，父 Agent 只接收结构化摘要与引用。

## 9. 取消与恢复

取消先更新 Task 版本，再通知 Worker。重启扫描过期 Lease、running Task、waiting 条件和 unknown ToolCall；旧 Attempt abandoned，确认安全后重新排队。

父取消以条件更新级联所有非终态 Child Task/Turn，只阻止后续执行，不声称撤销
已发生副作用。子 Agent 的高风险审批绑定原始 Principal；子 Turn 可等待用户，
父 Turn 继续等待 Join。

`subagent_manage list|status` 返回 Delegation、Child Task/Turn/Attempt、实际角色、Toolset、
预算、用量、结果引用与错误的结构化快照。显式取消 Delegation 会持久化 `cancelled` 结果、
满足等待条件并恢复仍在等待的父 Turn；因父 Task/Turn 自身取消触发的级联使用
`resume_parent=false`，不得把已取消父 Turn 重新排队。`join_policy=any` 命中首个结果后取消
其余非终态子执行。

## 10. 公平性与资源

队列按优先级、scheduled_at 和 aging 排序。为即时 Turn 保留资源；Connector、主动推送和 Drift 分别设置并发池，避免后台任务占满模型或 SQLite 写入。

## 11. 指标与测试

指标：队列长度、等待时间、Lease 丢失、重试、Misfire、执行时长和失败分类。

测试覆盖：双 Worker 竞争、旧 Lease 提交、重启、DST、重复 Fire、等待审批、子任务失败、Duration 解析边界值、"every" 短语在跨 DST 行为下的正确性。
