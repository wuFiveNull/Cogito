# 05：Task、Scheduler、Checkpoint 与 Delegation

## 目标

使后台任务、排程、租约、恢复和 Agent 委派在没有 `tasks`、`task_attempts`、`schedules` 或 checkpoint 表时仍可正确运行。

## 事件与流

- Task 流：`task.created|scheduled|leased|lease_renewed|waiting_*|retry_scheduled|completed|failed|cancelled`。
- TaskAttempt 流：`task.attempt.started|completed|failed|cancelled|abandoned`。
- Checkpoint 只用 checkpoint Event 的 payload 引用表达，不以表行作为真相。
- Schedule 流必须新增或补齐创建、修改、启停、触发、misfire 和 next-fire 更新事件。
- Delegation 使用父 Turn、子 Task、子 Turn 的 correlation/causation Event，不增加关系状态表。

## 实施步骤

1. 先让 TaskRepository/Event projection 覆盖创建、筛选、领取、续租、终结和恢复；移除 `event_sourced` 运行时开关，Event-only 成为唯一实现。
2. 将 TaskDispatcher/Worker 的抢占和 heartbeat 改为 expected-version 追加；过期 lease 的恢复只扫描流。
3. 将 Scheduler 的下一次触发、misfire 和幂等生成任务改为 Schedule replay；同一 schedule/fire window 必须得到同一 task idempotency key。
4. 将 Delegation Lifecycle 的父子更新替换为因果 Event；父任务恢复时 replay 子任务和子 Turn 的终结状态。
5. 删除 task/schedule/checkpoint 表 fallback 和对应查询 API 拼接。

## 验证

覆盖多 Worker 抢占、并发 heartbeat、lease 过期、checkpoint 恢复、misfire、重复 tick、委派取消、父子崩溃恢复。阶段退出时删除测试库中任务/排程表后，Worker 和 Scheduler 回归仍通过。
