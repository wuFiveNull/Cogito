---
doc_id: "SESSION-CONTEXT"
title: "Session、上下文与顺序"
version: "1.1"
status: "active"
source_of_truth: true
layer: "functional-spec"
domain: "runtime"
authority: "session-context"
scope: "Session 边界、短期上下文、长期记忆共享与 Context Partition Lane"
tags:
  - "session"
  - "context"
  - "context-partition-lane"
depends_on:
  - "ARCH-OVERVIEW"
  - "DOMAIN-CONTRACTS"
related_docs:
  - "ACCESS-DELIVERY"
  - "AGENT-COGNITION"
  - "EXECUTION-LIFECYCLE"
language: "zh-CN"
---

# Session、上下文与顺序

> **文档编号**：SESSION-CONTEXT  
> **适用范围**：Session 边界、短期上下文、长期记忆共享与 Context Partition Lane  
> **权威边界**：本文是 Session 解析、短期上下文隔离和会话内排序的权威来源。  
> **关联文档**：DOMAIN-CONTRACTS, ACCESS-DELIVERY, AGENT-COGNITION, EXECUTION-LIFECYCLE

## 1. 设计原则

1. Session 根据 Channel、平台 Conversation、可选 Thread 和多用户隔离策略决定。
2. 不同 Session 不共享短期消息、Conversation Summary 或 Context Snapshot。
3. 同一 Principal 的不同 Session 可以共享经过 Scope 校验的长期 Memory、Preference 和 Goal。
4. 不支持把多个 Channel 自动或手工绑定到同一个短期 Session。

## 2. Session 解析

先计算稳定的 Context Partition，再选择当前 Session generation：

```text
base_key = channel_instance_id + platform_conversation_id + optional_thread_id

private:
  context_partition_key = base_key + sender_principal_id

group/thread with per-user isolation:
  context_partition_key = base_key + sender_principal_id

group/thread with shared context:
  context_partition_key = base_key + "shared"

session_key = context_partition_key + reset_generation
```

Channel Driver 声明平台 Conversation/Thread 边界；Core 配置决定同一群聊或 Thread 是否再按发送者隔离：

```text
private chat       平台私聊 ID
group              群 ID
threaded channel   群 ID + thread ID
web                本地 Web conversation ID
```

SessionPolicy：

```python
class SessionPolicy(Protocol):
    async def resolve(
        self,
        channel: ChannelRef,
        conversation: Conversation,
        message: Message,
    ) -> SessionRef: ...
```

默认 `group_sessions_per_user=true`、`thread_sessions_per_user=false`。配置变化只影响新 Session generation，不把已有短期历史迁移到另一 Partition。跨 Channel 连续性只能通过长期 Memory 和显式 Task 实现。

## 3. 短期上下文

短期上下文只读取当前 `session_id` 下的数据：

```text
Recent Messages
Conversation Summary
Turn Checkpoints
Session-local temporary facts
```

Context Snapshot 必须记录：

```text
session_id
conversation_version
message_upper_bound
summary_id
selected_memory_ids
selection_policy_version
token_estimate
```

Snapshot 创建后不可因为新消息到达而改变。

## 4. 长期认知共享

长期数据按 Principal 和 Scope 检索：

```text
owner_global       Owner 各 Session 可见
principal_global   同一 Principal 各 Session 可见
channel_scoped     指定 Channel 可见
conversation_scoped 指定 Conversation 可见
session_scoped     只在当前 Session 可见
```

群聊和外部用户始终先经过 Principal、Trust Label 和 Policy 过滤，不能因为共享 Owner Memory Store 而获得 Owner 私有记忆。共享 Session 只共享短期对话文本；长期 Memory 检索仍以当前消息发送者 Principal 为主体。Conversation Summary 必须保留各消息发送者引用，不能把群聊陈述归因给 Owner。

## 5. Context Partition Lane

排序执行权绑定稳定的 `context_partition_key`，而不是整个平台 Conversation。每用户隔离的群聊允许不同用户 Partition 并行；共享 Session 的所有用户进入同一 Partition 串行。Session reset 只增加 generation，不改变 Partition Lane，因此旧、新 Session 不会并发提交上下文。

Lane 规则：

- 同一 context_partition_key 同时最多一个 `running` Turn；
- 入站顺序使用持久化 `receive_sequence`，不能依赖进程到达顺序；
- 取消和审批 Command 可以高优先级修改状态，但不能绕过版本检查提交上下文；
- Turn 进入等待态或终态立即释放 Lane；
- 长期 Task 不占用 Lane，需要写入当前会话时提交 Command 重新排队。

## 6. Session 重置

用户请求“新会话”时增加当前 Partition 的 `reset_generation` 并创建新的 Session；`context_partition_key` 保持不变。新 Session 不读取旧 Session 的短期 Message；旧 Session 仍保留用于审计和显式历史查询。

重置不会删除长期 Memory。删除或纠正长期 Memory 必须使用独立 Command。

## 7. 禁止行为

- 不根据显示名称合并 Session；
- 不自动把两个 Channel 的近期消息拼成一个 Prompt；
- 不让后台摘要跨 Session 覆盖短期 Conversation Summary；
- 不使用仅存在于进程内的锁作为排序事实；
- 不在 Session 切换后继续提交旧 Context Snapshot 生成的结果。
