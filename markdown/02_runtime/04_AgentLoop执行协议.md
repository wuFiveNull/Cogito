---
doc_id: "AGENT-LOOP"
title: "Agent Loop 执行协议"
version: "1.0"
status: "active"
source_of_truth: true
layer: "functional-spec"
domain: "runtime"
authority: "agent-loop"
scope: "Agent Loop 迭代、Tool 调用、终止、Checkpoint 和失败处理"
tags: ["agent-loop", "tool-calling", "checkpoint"]
depends_on: ["EXECUTION-LIFECYCLE", "DOMAIN-CONTRACTS"]
related_docs: ["MODEL-ADAPTER", "RETRIEVAL-CONTEXT", "TOOL-SANDBOX"]
language: "zh-CN"
---

# Agent Loop 执行协议

## 1. 职责

Agent Loop 将不可变 Context Snapshot 转换为最终回复、Tool 调用、Memory Candidate 或 Task Proposal。它不负责数据库提交、平台发送、权限授予和长期等待。

## 2. LoopState

```text
turn_id
attempt_id
iteration_no
context_snapshot_id
messages
tool_catalog_version
completed_tool_call_ids
pending_tool_calls
partial_output_ref
usage
finish_reason
```

LoopState 只存在于当前 Attempt；可恢复字段通过 Turn Checkpoint 持久化。

## 3. 单轮协议

```text
build ModelRequest
→ ModelProvider.generate/stream
→ normalize ModelResponse
→ validate output
→ final_response | tool_calls | task_proposals | fail
```

模型输出只能是：

```text
FinalResponse(content_parts)
ToolCalls(calls[])
StructuredDecision(data)
Refusal(reason)
InvalidOutput(error)
```

同一轮既包含最终回复又包含 Tool Call 时，默认先执行 Tool，忽略未确认的最终文本；Provider 明确支持并经过策略允许时才接受组合输出。

## 4. Tool Call

每个 Tool Call 依次经过 Registry 解析、参数 Schema、Policy、Approval、Budget 和 Tool Executor。结果按原 `tool_call_id` 和请求顺序加入下一轮消息。

并行 Tool 仅在以下条件全部满足时启用：

- Provider 声明支持；
- Tool Manifest 声明彼此无顺序依赖；
- 不竞争同一目标资源；
- 并发数不超过预算；
- 任一副作用 Tool 都有独立幂等键。

并行结果按请求序号稳定合并，不按完成时间改变 Prompt 顺序。

## 5. 输出校验与修复

参数或结构化输出无效时：

1. 保存安全错误摘要；
2. 在预算允许时进行最多一次同 Provider 修复；
3. 修复请求不执行任何 Tool；
4. 再次失败则返回明确失败或降级文本。

未知 Tool、越权 Tool 和 Policy Denied 不通过重复提示绕过。

## 6. 终止条件

按优先级检查：

```text
cancel requested
→ approval/wait required
→ hard resource limit
→ valid final response
→ provider terminal error
→ max iterations/tool calls/runtime
→ loop repetition detected
```

重复签名为 `tool_name + canonical_arguments + relevant_context_version`。连续达到配置阈值后终止并记录 `loop_detected`。

## 7. Checkpoint

以下边界写 Checkpoint：

- Model 请求完成且输出已规范化；
- 副作用 Tool 执行前的意图已持久化；
- Tool Receipt 写入后；
- 进入 Approval/外部等待前；
- 形成最终回复但尚未提交 Turn 前。

恢复创建新 RunAttempt，并从最后一个完整 Checkpoint 重建 LoopState。模型生成过程本身不从 Token 中间恢复。

## 8. 错误与恢复

| 错误 | 处理 |
|---|---|
| provider_timeout | 同 Attempt 有限重试或 fallback |
| invalid_output | 最多一次修复 |
| tool_validation | 作为结构化 ToolResult 返回模型或失败 |
| policy_denied | 不重试，返回安全说明 |
| side_effect_unknown | Turn waiting_external，先 reconcile |
| budget_exhausted | 生成可解释的受限回复 |

## 9. 配置

```text
max_iterations
max_tool_calls
max_parallel_tools
max_repeated_tool_signature
max_runtime
max_total_tokens
output_repair_attempts = 1
```

## 10. 可观察性

每轮记录模型、耗时、Token、输出类型、Tool 数量、终止原因和 Checkpoint ID，不保存私有思维链。

## 11. 验收测试

- 最终回复无需 Tool；
- 多轮 Tool 调用顺序稳定；
- 并行 Tool 结果乱序完成仍稳定合并；
- 无效参数不能到达 Executor；
- Approval 后创建新 Attempt 恢复；
- 重复 Tool 循环被终止；
- 崩溃点不重复副作用。

