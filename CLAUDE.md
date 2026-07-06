# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库定位

本仓库是**主动式个人 Agent（Cogito）的架构知识库**，不是可执行代码项目。包含分层架构文档、领域模型、运行时协议和功能规范，共 35 份有效文档，语言为中文。

`reference/` 目录包含三个参考实现的项目源码（LangBot、hermes-agent、akashic-agent），用于设计参考而非本仓库的构建产物。

## 文档系统

权威索引文件是 `manifest.json`，包含每份文档的 `doc_id`、文件路径、版本、层级和权威范围。

文档按层级组织在 `markdown/` 下：

| 目录 | 层级 | 内容 |
|---|---|---|
| `00_guides/` | guide | 导航索引、统一术语表、ADR 指南 |
| `01_architecture/` | architecture | 系统边界、领域契约、全局不变量、依赖规则 |
| `02_runtime/` | functional-spec | 执行生命周期、Session、Agent Loop、检索、记忆、模型适配 |
| `03_capabilities/` | functional-spec | Tool、MCP、Plugin、Sandbox |
| `04_background/` | functional-spec | Task、Event/Outbox、Connector、主动推送、Drift |
| `05_interaction/` | functional-spec | LangBot Bridge、消息持久化、投递、审批、流式 |
| `06_infrastructure/` | implementation-spec | 存储、配置、数据库 Schema、本地部署 |
| `07_quality/` | quality-spec | 安全、可观察性、审计、测试回放 |

### 权威层级

冲突时按以下顺序解决：architecture（系统边界）> functional-spec（字段/状态/失败）> implementation-spec（Schema/配置/操作）。高层文档定义边界，细化文档定义边界内的细节。

### 文档引用方式

每份 Markdown 有 YAML 元数据和稳定 `doc_id`。使用 `doc_id / heading path` 格式引用设计决策。完整的 doc_id 列表见 `manifest.json`。

## 系统架构概要

Cogito 是一个**本地优先、单所有者的主动式个人 Agent**，不是多租户 SaaS 或通用插件市场。

### 进程边界

```
interaction-web → agent-api ↔ agent-worker → sqlite + payloads
                         ↕
                 channel-gateway (LangBot)
                         ↕
               model / MCP / external services
```

- `agent-api`：拥有即时 Turn 和 Command
- `agent-worker`：通过 Lease 执行 Task、Delivery、Outbox 和后台维护
- `channel-gateway`：拥有平台连接，不拥有 Agent 状态；推荐使用 LangBot 作为独立 Gateway
- `interaction-web`：Dashboard 和 Web Channel 前端，只使用 Query/Command/Stream API

### 三类运行模型

1. **Turn Orchestrator**：处理即时交互（低延迟、流式输出、可取消）
2. **Durable Job Runner**：处理跨重启的长期工作（持久化 Lease、Checkpoint、重试）
3. **Event Bus**：传播已发生事实（Consumer 必须幂等，不可伪装同步返回值）

### 核心领域概念

`Principal → Endpoint → Conversation → Session → Message / Turn → RunAttempt`；Task 和 Delivery 独立于 Turn 生命周期；MemoryItem 是带来源和置信度的长期认知事实。

### 全局不变量

SQLite 是唯一事务事实源；所有入站消息有稳定幂等键；同一 Session 内改变上下文的 Turn 不并发提交；外部副作用必须经过 Policy Engine 并产生 Receipt；模型输出不等于授权结果；需要跨重启等待的工作必须持久化为 Task 或 Approval。

## 任务到文档的映射

AGENTS.md 中定义了常见任务对应的文档组合。修改设计时，先读 `manifest.json` 找到相关文档，再按需加载。关键映射示例：

- 修改 Agent Loop：`AGENT-LOOP` + `EXECUTION-LIFECYCLE` + `MODEL-ADAPTER`
- 新增 Tool/MCP：`TOOL-SANDBOX` + `CAPABILITY-PLUGINS` + `SECURITY-OBS`
- 修改数据库：`DATABASE-SCHEMA` + `STORAGE-DATA` + `DOMAIN-CONTRACTS`
- 修改 LangBot 集成：`LANGBOT-BRIDGE` + `ACCESS-DELIVERY` + `MESSAGE-PERSISTENCE`

## 术语约定

使用统一术语表（`GLOSSARY`）中的定义。关键区分：
- **Turn** 是用户意图逻辑单元，**RunAttempt** 是执行尝试，没有独立 Run 层
- **Task** 是可持久化后台工作，**TaskAttempt** 是 Worker 的一次执行占用
- **Session** 是短期上下文边界（≠ 平台 Conversation）
- **Delivery** 独立于 Turn/RunAttempt，发送失败不回滚推理结果
- **Event** 表示事实，**Command** 表示变更请求，不可互换

## 核心规则

- conda activate cogito激活python环境。
- 每次开发完成都要git push 到我的github仓库。
- 你可以使用我的真实模型进行测试和开发，前提是省着点用，同时，尽量不要使用规则化，正则化的匹配，要足够智能。
- 目前将workspace放到.workspace/下
- 其他部件需要增删改查数据库，必须调用数据库的服务
- 不要修改与当前任务无关的代码。
- 优先修改现有模块，不要随意创建新的抽象层。
- 当前项目配置以及apikey明文存放到config.toml中
- 未经明确要求，不得改变公共 API 的行为。
- 遇到需求不明确时，先说明假设，不要静默猜测。