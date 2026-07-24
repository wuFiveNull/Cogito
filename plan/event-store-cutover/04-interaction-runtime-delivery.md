# 04：交互、运行时、模型、工具、审批与投递

## 目标

完成用户消息从接收、执行到外部回复的纯 Event 链路，并移除 Turn/Attempt/Delivery 运行时的旧表 fallback。

## 标准因果链

`interaction.message.accepted` → 身份/会话事实 → `runtime.turn.accepted|queued` → `runtime.turn.started` + `runtime.attempt.started` → `runtime.context.assembled` → `model.call.*` / `tool.call.*` / `approval.*` → `runtime.response.generated` → `delivery.requested` → `delivery.*` → `runtime.attempt.completed` + `runtime.turn.completed`。

所有节点沿用同一 `trace_id`。一次模型或工具调用创建 span；`parent_span_id` 表示嵌套，`causation_id` 指向直接触发的 Event。

## 实施步骤

1. 将 Inbound、Session Resolver、Dispatcher、AgentRunner、Completion、Streaming Delivery 和 Recovery 的所有状态读写改为 replay + 预期版本 append。
2. Turn 领取必须原子追加 started/attempt started；完成、失败、取消、等待用户、等待外部和重试追加对应后继事件。
3. 模型只写开始、终结、用量、摘要和受控 payload 引用；不得记录 token 流或完整响应。
4. 工具和审批使用阶段 03 副作用协议；审批恢复依据 approval 流，而非审批表。
5. Delivery 请求携带目标快照的安全摘要与 payload 引用；流式编辑只保留占位和终结生命周期，不持久化每个 delta。
6. 恢复服务从 Turn/Attempt/Delivery 流寻找过期 lease、孤儿流式投递和 unknown Delivery，追加恢复 Event。

## 测试与退出条件

覆盖普通回复、模型失败重试、审批、工具失败、流式中断、Delivery unknown、回执迟到、重启恢复和 Trace 因果树。完成后 `dispatcher.py`、相关 repositories、recovery 和查询路径不允许访问 `turns`、`run_attempts`、`deliveries`、`model_calls`、`tool_calls`、`approvals`、`traces` 或 `spans`。
