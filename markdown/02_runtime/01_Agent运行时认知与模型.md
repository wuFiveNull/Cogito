---
doc_id: "AGENT-COGNITION"
title: "Agent 运行时、认知与模型"
version: "1.2"
status: "active"
source_of_truth: true
layer: "subsystem-overview"
domain: "runtime"
authority: "cognition-boundaries"
scope: "Turn、Agent Loop、Prompt、Context、Memory、Preference、Goal、Model/Embedding Provider"
tags:
  - "agent-runtime"
  - "memory"
  - "context"
  - "prompt"
  - "model"
  - "embedding"
depends_on:
  - "ARCH-OVERVIEW"
  - "DOMAIN-CONTRACTS"
  - "RUNTIME-FLOWS"
related_docs:
  - "CAPABILITY-PLUGINS"
  - "PROACTIVE-TASKS"
  - "SECURITY-OBS"
  - "EXECUTION-LIFECYCLE"
  - "SESSION-CONTEXT"
  - "AGENT-LOOP"
  - "RETRIEVAL-CONTEXT"
  - "MEMORY-LIFECYCLE"
  - "MODEL-ADAPTER"
language: "zh-CN"
---

# Agent 运行时、认知与模型

> **文档编号**：AGENT-COGNITION  
> **适用范围**：Turn、Agent Loop、Prompt、Context、Memory、Preference、Goal、Model/Embedding Provider  
> **权威边界**：本文是其范围内的规范性来源；总体架构文档只负责概念与边界。  
> **细化规范**：Agent Loop、检索、长期记忆和模型 Provider 分别以 `AGENT-LOOP`、`RETRIEVAL-CONTEXT`、`MEMORY-LIFECYCLE`、`MODEL-ADAPTER` 为准。  
> **关联文档**：CAPABILITY-PLUGINS, PROACTIVE-TASKS, SECURITY-OBS

## 阅读说明

**目的**：定义一轮 Agent 执行、模型能力协商、Prompt/Context、Memory、Preference 和 Goal。

**边界**：不负责 Channel 协议、后台调度或最终投递。

**建议读取方式**：修改 Agent Loop、Memory、模型路由或上下文构建时读取。

## 1. Turn Orchestrator

### 1.1 职责

- 为已取得 Lane 的 Turn 创建 RunAttempt；
- 获取 Context Partition Lane；
- 加载 Session；
- 调用 Context Builder；
- 调用 Agent Runtime；
- 处理 Tool 请求；
- 持久化检查点；
- 创建 Memory Candidate；
- 生成 AgentReply；
- 写入 Delivery Outbox；
- 发布 Turn 相关 Event；
- 处理取消、超时、审批和恢复。

### 1.2 执行步骤

```text
1. validate_input
2. resolve_identity
3. acquire_conversation_lane
4. create_run_attempt
5. build_context
6. run_agent_loop
7. persist_messages_and_candidates
8. build_reply
9. enqueue_delivery
10. complete_attempt_turn_and_release_lane
```

步骤 5-8 不在同一个长事务中执行。每个可恢复点写入属于 Turn、带来源 Attempt 的 Checkpoint。

### 1.3 Checkpoint

Checkpoint 至少包含：

```text
agent_iteration
conversation_snapshot_version
prompt_inputs_ref
tool_call_states
partial_reply_ref
pending_approval_id
resource_usage
```

恢复时重新验证：

- Session 和 Context Snapshot 版本是否仍有效；
- Tool Receipt 是否已存在；
- Reply 是否已入 Outbox；
- 用户是否请求取消；
- Approval 是否过期。

### 1.4 Turn 状态

Turn 持有逻辑状态；RunAttempt 只表示一次执行占用。等待、失败后恢复或显式重试创建新的 RunAttempt，不存在额外 Run 层。完整状态机和提交条件见 `EXECUTION-LIFECYCLE / 2. 状态机`。

### 1.5 降级

模型不可用时可按策略：

- 切换备用 Model Provider；
- 返回明确错误；
- 创建延迟重试任务；
- 使用规则回复处理简单命令。

不应静默生成与用户请求无关的模板回复。

---

## 2. Agent Runtime

### 2.1 职责

Agent Runtime 负责：

- 解析当前任务；
- 构建模型请求；
- 执行受限 Agent Loop；
- 选择 Skill 和 Tool；
- 处理结构化 Tool Call；
- 汇总 Tool Result；
- 生成用户可见回复；
- 产出任务建议和记忆候选。

Agent Runtime 不负责：

- 直接写数据库表；
- 直接发送 Channel 消息；
- 读取未授权 Secret；
- 绕过 Tool Policy；
- 保存完整私有推理链。

### 2.2 Agent 模式

```text
reactive    响应用户消息
proactive   处理主动候选并生成通知
scheduled   执行计划任务
maintenance 整理记忆、索引或系统数据
```

模式影响工具白名单、预算和 Prompt，不改变核心安全策略。

### 2.3 Agent Loop

```text
Context
→ Model Request
→ Assistant Output
   ├─ Final Response
   ├─ Tool Calls
   ├─ Approval Request
   └─ Task Proposal
→ Tool Policy
→ Tool Execution
→ Tool Results
→ Next Model Iteration
```

循环必须限制：

```text
max_iterations
max_tool_calls
max_repeated_tool_signature
max_total_tokens
max_runtime
```

检测相同 Tool + 参数重复调用时，应触发循环保护。

### 2.4 Prompt 构建

Prompt 分区：

```text
System Policy
Runtime Mode
User Profile Summary
Current Goals
Conversation Summary
Retrieved Memory
Recent Messages
External Untrusted Context
Available Tools
Current User Input
```

每个分区带来源和信任标签。外部内容使用明确边界包裹。

### 2.5 Model Capability 协商

Model Provider 必须声明：

```text
supports_streaming
supports_tools
supports_parallel_tools
supports_json_schema
supports_vision
supports_audio
supports_prompt_cache
context_window
max_output_tokens
```

Agent Runtime 根据能力选择：

- 原生 Tool Calling；
- 结构化文本解析；
- Vision 降级；
- 分块和摘要；
- 串行 Tool 调用。

### 2.6 输出

AgentAttemptResult：

```text
final_content
assistant_messages
tool_calls
memory_candidates
task_proposals
usage
finish_reason
decision_summary
```

`decision_summary` 是可审计摘要，不是私有思维过程。

---

## 3. Cognition

### 3.1 组成

Cognition 包含：

- Canonical Memory Store；
- Memory Extractor；
- Memory Retriever；
- Memory Ranker；
- Memory Consolidator；
- Preference View；
- Goal View；
- Context Builder；
- Conversation Summarizer。

### 3.2 Canonical Memory Store

系统始终保留自己的标准 MemoryItem。外部记忆服务可以作为 Retriever 或 Enricher，但不能成为唯一事实源。

### 3.3 Memory 写入流程

```text
Turn/Task Output
→ Extract Candidates
→ Normalize
→ Sensitive Data Policy
→ Duplicate/Conflict Check
→ candidate
→ Auto-confirm Rule or User Confirmation
→ confirmed
```

默认不会将模型提取结果直接作为高置信永久事实。

### 3.4 冲突处理

新 Memory 与旧 Memory 可能：

```text
support
contradict
supersede
refine
unrelated
```

Preference 和 Goal 应保留时间性。例如“最近希望少收到通知”不能永久覆盖长期偏好。

### 3.5 检索

MemoryQuery：

```text
query_text
kinds
scope
principal_id
session_id
time_range
minimum_confidence
max_items
token_budget
```

检索流程：

```text
filter
→ lexical/vector retrieval
→ deduplicate
→ rank
→ diversity
→ token budget
→ MemoryResult
```

MemoryResult 必须包含来源、置信度和时间，不能只返回拼接文本。

### 3.6 Preference 与 Goal

PreferenceService 和 GoalService 是 Canonical Memory 的领域视图：

- Preference 提供当前有效偏好和冲突解释；
- Goal 提供活动目标、优先级、截止时间和进度；
- 不分别维护互不一致的事实数据库。

### 3.7 Context Builder

Context Builder 输入：

```text
Turn
Session
Recent Messages
MemoryResult
Preferences
Goals
Task State
Channel Capabilities
Resource Budget
```

输出结构化 `AgentContext`，并记录各内容段占用 Token 和来源。

---

## 4. Model 与 Embedding Provider

### 4.1 ModelProvider 接口

```python
class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def stream(self, request: ModelRequest): ...
    def capabilities(self) -> ModelCapabilities: ...
    async def health(self) -> HealthStatus: ...
```

### 4.2 ModelRequest

```text
model_role
messages
tools
response_schema
temperature
max_output_tokens
timeout
cache_policy
trace_context
```

### 4.3 路由

ModelRouter 可按：

- 任务类型；
- 隐私等级；
- 模态；
- 成本；
- 延迟；
- Context 长度；
- Provider 健康状态；

选择模型。

### 4.4 失败切换

只在以下条件切换：

- 请求尚未产生外部副作用；
- Response 未部分交付或可安全重建；
- 模型语义差异可接受；
- 不超过总预算。

### 4.5 Embedding

Embedding 记录：

```text
provider
model
dimension
normalization
created_at
source_hash
```

更换模型后不直接混用不同向量空间。需要新索引版本或后台重建。

---
