---
doc_id: "DOCSET-INDEX"
title: "文档索引与 Agent 阅读指南"
version: "2.0"
status: "active"
source_of_truth: true
layer: "guide"
domain: "documentation"
authority: "document-index"
scope: "文档层级、导航、权威边界和检索规则"
tags: ["index", "agent-reading", "documentation"]
depends_on: []
related_docs: ["ARCH-OVERVIEW", "GLOSSARY", "ADR-GUIDE"]
language: "zh-CN"
---

# 文档索引与 Agent 阅读指南

## 1. 文档层级

```text
guide                 导航、术语、决策流程
architecture          系统边界、领域语言、全局不变量
subsystem-overview    子系统职责和上下游关系
functional-spec       单一功能的对象、状态机、事务和恢复
implementation-spec   Schema、配置和运行操作
quality-spec          安全、可观察性、测试和评估
```

权威冲突按以下顺序解决：系统边界以 architecture 为准；功能字段和状态以 functional-spec 为准；物理 Schema、配置和操作以 implementation-spec 为准；综合总览不得覆盖细化规范。

## 2. 目录

### 2.1 Guides

- [文档索引与阅读指南](00_文档索引与Agent阅读指南.md) — `DOCSET-INDEX`
- [统一术语表](01_术语表.md) — `GLOSSARY`
- [架构决策记录指南](02_架构决策记录指南.md) — `ADR-GUIDE`

### 2.2 Architecture

- [总体架构](../01_architecture/00_总体架构设计.md) — `ARCH-OVERVIEW`
- [核心领域模型与数据契约](../01_architecture/01_核心领域模型与数据契约.md) — `DOMAIN-CONTRACTS`
- [全局不变量](../01_architecture/02_全局不变量.md) — `GLOBAL-INVARIANTS`
- [系统边界与依赖规则](../01_architecture/03_系统边界与依赖规则.md) — `SYSTEM-BOUNDARIES`

### 2.3 Runtime

- [运行机制总览](../02_runtime/00_运行机制与端到端流程.md) — `RUNTIME-FLOWS`
- [Agent认知总览](../02_runtime/01_Agent运行时认知与模型.md) — `AGENT-COGNITION`
- [执行生命周期与恢复](../02_runtime/02_执行生命周期与恢复.md) — `EXECUTION-LIFECYCLE`
- [Session上下文与顺序](../02_runtime/03_Session上下文与顺序.md) — `SESSION-CONTEXT`
- [Agent Loop执行协议](../02_runtime/04_AgentLoop执行协议.md) — `AGENT-LOOP`
- [检索与上下文装配](../02_runtime/05_检索与上下文装配策略.md) — `RETRIEVAL-CONTEXT`
- [长期记忆生命周期](../02_runtime/06_长期记忆生命周期.md) — `MEMORY-LIFECYCLE`
- [模型适配层](../02_runtime/07_模型适配层.md) — `MODEL-ADAPTER`

### 2.4 Capabilities

- [能力、工具、MCP与插件总览](../03_capabilities/00_能力工具MCP与插件.md) — `CAPABILITY-PLUGINS`
- [Tool执行与Sandbox](../03_capabilities/01_Tool执行与Sandbox.md) — `TOOL-SANDBOX`

### 2.5 Background

- [数据、事件、主动系统与任务总览](../04_background/00_数据事件主动系统与任务.md) — `PROACTIVE-TASKS`
- [Task与Scheduler](../04_background/01_Task与Scheduler.md) — `TASK-SCHEDULER`
- [Event、Inbox与Outbox](../04_background/02_EventInbox与Outbox.md) — `EVENT-OUTBOX`
- [Connector数据摄取](../04_background/03_Connector数据摄取.md) — `CONNECTOR-INGESTION`
- [主动推送与后台空闲处理](../04_background/04_主动推送与后台空闲处理.md) — `PROACTIVE-IDLE`

### 2.6 Interaction

- [接入、交互、身份与投递总览](../05_interaction/00_接入交互身份与投递.md) — `ACCESS-DELIVERY`
- [审批与命令](../05_interaction/01_审批与命令.md) — `APPROVAL-COMMANDS`
- [流式投递](../05_interaction/02_流式投递.md) — `STREAMING-DELIVERY`
- [消息持久化与历史](../05_interaction/03_消息持久化与历史.md) — `MESSAGE-PERSISTENCE`
- [LangBot Bridge契约](../05_interaction/04_LangBotBridge契约.md) — `LANGBOT-BRIDGE`

### 2.7 Infrastructure

- [存储、一致性与数据治理总览](../06_infrastructure/00_存储一致性与数据治理.md) — `STORAGE-DATA`
- [部署、配置、测试与治理总览](../06_infrastructure/01_部署配置测试与架构治理.md) — `OPS-GOVERNANCE`
- [配置与Profile](../06_infrastructure/02_配置与Profile.md) — `CONFIG-PROFILES`
- [数据库Schema与Migration](../06_infrastructure/03_数据库Schema与Migration.md) — `DATABASE-SCHEMA`
- [本地部署与运行手册](../06_infrastructure/04_本地部署与运行手册.md) — `LOCAL-OPERATIONS`

### 2.8 Quality

- [安全、可观察性与资源治理总览](../07_quality/00_安全策略可观察性与资源治理.md) — `SECURITY-OBS`
- [可观察性与审计](../07_quality/01_可观察性与审计.md) — `OBSERVABILITY-AUDIT`
- [测试、回放与质量评估](../07_quality/02_测试回放与质量评估.md) — `TEST-EVALUATION`

## 3. 阅读路径

首次理解：`ARCH-OVERVIEW → DOMAIN-CONTRACTS → GLOBAL-INVARIANTS → 对应子系统总览`。

实现功能：先读对应 functional-spec，再读其 `depends_on` 和所需 implementation-spec。修改跨模块契约时必须检查 `DOMAIN-CONTRACTS`、`RUNTIME-FLOWS`、`SYSTEM-BOUNDARIES` 和受影响规范。

## 4. Agent 检索规则

1. 先读 `manifest.json` 或本索引；
2. 只加载当前任务相关文档；
3. 使用 `doc_id / heading path` 引用；
4. 不把示例、建议或测试 Fixture 当作已实现事实；
5. 不从一个模块内部实现推导另一个模块责任；
6. 字段、状态、失败和恢复优先查 functional-spec；
7. 数据库和配置细节优先查 implementation-spec。

## 5. 完整性标准

每份功能规范应包含职责、对象字段、状态机、正常流程、事务、幂等并发、错误重试、恢复、配置、可观察性、数据库映射和验收测试。缺少关键项时必须明确标记“不适用”或引用唯一权威文档。

