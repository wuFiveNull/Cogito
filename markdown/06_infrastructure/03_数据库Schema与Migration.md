---
doc_id: "DATABASE-SCHEMA"
title: "数据库 Schema 与 Migration"
version: "1.1"
status: "active"
source_of_truth: true
layer: "implementation-spec"
domain: "infrastructure"
authority: "database-schema"
scope: "SQLite 表、主外键、唯一约束、索引、事务和 Migration"
tags: ["sqlite", "schema", "migration"]
depends_on: ["STORAGE-DATA", "DOMAIN-CONTRACTS"]
related_docs: ["MESSAGE-PERSISTENCE", "EXECUTION-LIFECYCLE", "EVENT-OUTBOX"]
language: "zh-CN"
---

# 数据库 Schema 与 Migration

## 1. SQLite 模式

启用 WAL、foreign_keys、busy_timeout；默认 `synchronous=NORMAL`，关键备份/迁移可使用 FULL。数据库中的时间统一保存为 UTC epoch milliseconds（`INTEGER`）；跨进程 Contract 使用带时区的 RFC 3339 字符串，并在边界转换。禁止同一列混用字符串和整数。ID 使用应用生成的稳定字符串。

## 2. 表分组

```text
identity: principals,endpoints,conversations,sessions
message: messages,message_revisions,content_parts,inbound_inbox
execution: turns,run_attempts,turn_checkpoints,model_calls
task: tasks,task_attempts,task_checkpoints,schedules,scheduled_fires
event: events,outbox_events,event_consumptions,dead_letters
delivery: deliveries,delivery_attempts,delivery_receipts
capability: tool_calls,side_effect_receipts,approvals,commands
cognition: memory_items,memory_relations,memory_embeddings,summaries
connector: connectors,connector_cursors,raw_items,normalized_items
ops: payload_objects,traces,spans,audit_records,config_versions
```

不创建 `runs` 表。

## 3. 通用字段

可变聚合包含 `version INTEGER NOT NULL`；状态对象包含 created/updated/terminal 时间；软删除对象包含 deleted_at。JSON 只用于非关键 Metadata，状态、外键、幂等键和查询字段必须独立列。

契约默认值与物理 Schema 必须一致。特别是 `content_parts.size` 为
`INTEGER NOT NULL DEFAULT 0`；跨进程 DTO 和进程内值对象不得用 `None`
覆盖该默认。Repository 在写入前仍需归一化，以兼容旧 Adapter。

## 4. 关键外键

- Session → Conversation；Message → Conversation/Session；
- RunAttempt/Checkpoint → Turn；
- TaskAttempt/Checkpoint → Task；
- ToolCall → RunAttempt 或 TaskAttempt，二选一约束；
- Delivery → Message 可空（provisional），最终化后非空；
- Memory source 使用受约束 source_type/source_id 或关系表；
- Payload 引用统一指向 payload_objects。

## 5. 唯一约束

```text
(channel_instance_id,platform_event_id)
(conversation_id,context_partition_key,reset_generation)
(conversation_id,receive_sequence)
(message_id,revision_no)
(message_id,platform_edit_id)
(turn_id,attempt_no)
(task_id,attempt_no)
(task_type,idempotency_key)
(schedule_id,scheduled_fire_time)
(aggregate_type,aggregate_id,aggregate_version)
(consumer_name,event_id)
(actor_principal_id,command_type,idempotency_key)
(tool_id,idempotency_key)
(delivery_target_key,idempotency_key)
```

## 6. 索引

按队列条件建立部分索引：queued Task、pending Outbox、ready Delivery、pending Approval。历史查询索引 Conversation+sequence；Memory 索引 Principal+scope+status+kind；Trace 索引 correlation/turn/task。

## 7. 事务

UnitOfWork 每次只修改少量聚合。Lease 使用条件 UPDATE+RETURNING 或事务内选择更新。外部调用不持有事务。Payload metadata 与业务引用同事务。

## 8. Migration

Migration 文件单调编号，包含 `up`、兼容检查、数据校验和必要回滚/导出说明。启动流程只自动执行标记为 online-safe 的小迁移；大型或破坏性迁移进入 maintenance 模式。

## 9. 展开/收缩

破坏性字段变化使用：新增字段/表→双读兼容→回填→切换写入→验证→后续版本删除旧字段。消息 Schema 与数据库 Schema 版本分开。

## 10. 备份和测试

迁移前 SQLite online backup，并固定 Payload manifest。测试覆盖空库、上一版本升级、重复启动、中断恢复、外键、唯一约束、查询计划和真实数据量级回填。
