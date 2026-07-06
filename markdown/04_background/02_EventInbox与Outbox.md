---
doc_id: "EVENT-OUTBOX"
title: "Event、Inbox 与 Outbox"
version: "1.1"
status: "active"
source_of_truth: true
layer: "functional-spec"
domain: "background"
authority: "event-delivery"
scope: "领域事件、Transactional Outbox、Consumer Inbox、顺序、重试和回放"
tags: ["event", "outbox", "inbox"]
depends_on: ["RUNTIME-FLOWS", "DOMAIN-CONTRACTS"]
related_docs: ["TASK-SCHEDULER", "DATABASE-SCHEMA"]
language: "zh-CN"
---

# Event、Inbox 与 Outbox

## 1. EventEnvelope

```text
event_id/type
aggregate_type/id/version
payload_ref
occurred_at
ingested_at
content_hash
schema_version
correlation_id/causation_id
origin/trust_label
trace_context
```

Event 使用过去式命名，不包含“请执行”语义。

## 2. 发布事务

Domain Service 在业务状态事务内将 Event 加入 Outbox。Commit 成功前不得对外发布；回滚时业务状态和 Outbox 一起消失。

```text
business update
+ aggregate version
+ outbox_event
COMMIT
```

## 3. Outbox 状态

```text
pending → leased → published
                 ├→ retry_scheduled
                 └→ dead_letter
```

Publisher 获取短 Lease，事务外投递，随后条件提交。Broker 不存在时可由 SQLite Consumer 直接读取，但仍使用同样语义。

## 4. 顺序

只保证同一 `aggregate_type + aggregate_id` 按 `aggregate_version` 有序。Publisher 领取时只能选择该 Aggregate 当前最小的未发布版本：若更小版本仍处于 pending、leased 或 retry_scheduled，则后续版本不可领取。并行 Publisher 可按 Aggregate Key Hash 分区，但同一分区串行提交发布结果。

不同聚合不承诺全局顺序。Consumer 仍需检查版本；发现版本缺口时延迟消费或重建状态，不能猜测缺失事件。若外部 Broker 无法提供分区内顺序，则系统只能声明“Consumer 侧重排”，不能宣称发布有序。

## 5. Inbox

唯一键 `(consumer_name,event_id)`。Consumer 在同一事务内完成派生状态更新、后续 Outbox 和 Inbox succeeded。重复事件返回已有结果。

## 6. Handler 边界

Handler 可以更新自身 Projection、创建 Command/Task 或记录指标。修改其他聚合必须调用对应 Command Handler；外部副作用必须创建 Delivery/Task，不在消费事务中直接调用网络。

## 7. 失败与死信

Schema 不支持、永久验证错误和超过最大尝试进入 dead letter。记录安全错误、重试次数和修复建议。修复后通过显式 Replay Command 重新产生消费尝试，原 Event 不修改。

## 8. Schema 演进

Consumer 声明支持版本范围。增加可选字段向后兼容；字段语义变化创建新版本。历史回放使用 Upcaster 转换到内部 Canonical Event，并记录转换版本。

## 9. 保留与回放

Outbox published 项可按保留期清理，但领域 Event 和审计保留由数据策略决定。回放指定 Event 范围、Consumer、dry-run 和副作用禁用模式，不复用生产 Inbox 结果。

## 10. 指标与测试

指标：Outbox backlog/age、发布延迟、重复率、Handler 失败、版本缺口和 dead letter。测试覆盖事务回滚、崩溃窗口、重复发布、多 Consumer 隔离、顺序缺口和回放不产生真实副作用。
