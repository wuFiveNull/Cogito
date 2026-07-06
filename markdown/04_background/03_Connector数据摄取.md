---
doc_id: "CONNECTOR-INGESTION"
title: "Connector 数据摄取"
version: "1.0"
status: "active"
source_of_truth: true
layer: "functional-spec"
domain: "background"
authority: "connector-ingestion"
scope: "Poll、Webhook、Cursor、原始归档、标准化、去重、撤回和回放"
tags: ["connector", "ingestion", "cursor"]
depends_on: ["PROACTIVE-TASKS", "TASK-SCHEDULER"]
related_docs: ["EVENT-OUTBOX", "PROACTIVE-IDLE"]
language: "zh-CN"
---

# Connector 数据摄取

## 1. 边界

Connector 只把外部事实带入系统，不执行外部写操作。所有来源数据默认 `external_untrusted`，结构化官方 API 也不能携带系统指令。

## 2. 对象

```text
ConnectorInstance
ConnectorCursor
IngestionBatch
RawItem
NormalizedItem
SourceEvent
```

Cursor 包含值、版本、上次成功/尝试、失败次数、next_poll_at 和 Lease。

## 3. Poll 协议

```text
Scheduler creates Poll Task
→ acquire connector lease
→ load cursor/conditional token
→ fetch bounded batch
→ archive raw payload
→ normalize and deduplicate
→ commit items/events/cursor/outbox
→ optional source acknowledge
```

Cursor 只在批次中所有已接受项安全持久化后推进。批次过大时按来源稳定顺序分段提交。

## 4. Webhook

Webhook 先验证来源签名（若平台支持）、大小和时间窗口，再写入 Inbound Inbox 与 Raw Payload，立即返回。Normalize 在 Task 中执行；重复 Event ID 不重复产生 SourceEvent。

## 5. 原始归档

保存 source、item ID、更新时间、抓取时间、HTTP 元数据、content hash 和 Payload 引用。原始内容不可原地覆盖；更新创建新版本并关联前一版本。

## 6. 标准化

Canonical Item 至少包含：来源类型、外部 ID、标题、正文/摘要、作者、事件时间、更新时间、链接、附件、Trust Label 和 Source Metadata。缺失字段保留为空，不从文本猜测稳定 ID。

## 7. 去重和变化

优先键：稳定外部 ID；其次外部 ID+版本；再次规范化 Hash。模糊相似度只用于关联建议。内容变化产生 `ConnectorItemUpdated`；来源明确删除产生 `Retracted`，不删除历史 Event。

## 8. Acknowledge

仅在本地 Commit 后调用来源 acknowledge。Acknowledge 失败可重试，不回滚本地事实；必须使用 batch/item 幂等标识。

## 9. 失败与限流

遵循 Retry-After、条件请求和指数退避。认证失败暂停 Connector 并通知用户；单个坏 Item 进入隔离队列，不永久阻塞整个 Cursor，但推进决定和跳过原因必须审计。

## 10. 回放

Normalize 版本升级时从 RawItem 生成新的派生版本。回放默认不触发主动推送和外部副作用；需要重新进入 Proactive Decision 时使用显式范围和 dry-run。

## 11. 配置与测试

配置批大小、频率、时间范围、允许来源、Payload 保留和并发。测试覆盖重复页、游标回退、更新、撤回、坏 Item、限流、崩溃、Webhook 重放和 Normalize 升级。

