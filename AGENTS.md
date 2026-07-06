# AGENTS.md

本目录是主动式个人 Agent 的架构知识库。

## 检索规则

1. 先读取 `manifest.json` 或 `markdown/00_guides/00_文档索引与Agent阅读指南.md`。
2. 只加载与当前任务直接相关的文档；需要系统边界时加载 `ARCH-OVERVIEW`。
3. `architecture` 负责系统边界；`subsystem-overview` 负责模块边界；`functional-spec` 负责字段、状态和失败；`implementation-spec` 负责 Schema、配置和运行。
4. 引用设计时使用 `doc_id / heading path`。
5. 修改跨模块契约时至少检查 `DOMAIN-CONTRACTS`、`RUNTIME-FLOWS` 和受影响专题。
6. 示例代码、建议表结构和配置片段不是已实现事实。
7. 不从一个模块的内部实现推导另一个模块的隐式责任。

## 常见任务映射

- 新增 Channel：`ACCESS-DELIVERY` + `DOMAIN-CONTRACTS` + `SECURITY-OBS`
- 修改 LangBot：`LANGBOT-BRIDGE` + `ACCESS-DELIVERY` + `MESSAGE-PERSISTENCE`
- 修改 Agent Loop：`AGENT-LOOP` + `EXECUTION-LIFECYCLE` + `MODEL-ADAPTER`
- 修改检索：`RETRIEVAL-CONTEXT` + `MEMORY-LIFECYCLE` + `SESSION-CONTEXT`
- 修改长期记忆：`MEMORY-LIFECYCLE` + `DOMAIN-CONTRACTS` + `DATABASE-SCHEMA`
- 修改 Turn/重试/恢复：`EXECUTION-LIFECYCLE` + `DOMAIN-CONTRACTS` + `RUNTIME-FLOWS`
- 修改 Session/上下文排序：`SESSION-CONTEXT` + `ACCESS-DELIVERY` + `AGENT-COGNITION`
- 修改审批或 Command：`APPROVAL-COMMANDS` + `DOMAIN-CONTRACTS` + `SECURITY-OBS`
- 修改流式回复：`STREAMING-DELIVERY` + `ACCESS-DELIVERY` + `RUNTIME-FLOWS`
- 新增 Tool/MCP：`TOOL-SANDBOX` + `CAPABILITY-PLUGINS` + `SECURITY-OBS`
- 新增 Connector：`CONNECTOR-INGESTION` + `EVENT-OUTBOX` + `TASK-SCHEDULER`
- 修改主动推送/Drift：`PROACTIVE-IDLE` + `TASK-SCHEDULER` + `STREAMING-DELIVERY`
- 修改消息持久化：`MESSAGE-PERSISTENCE` + `DATABASE-SCHEMA` + `LANGBOT-BRIDGE`
- 修改数据库：`DATABASE-SCHEMA` + `STORAGE-DATA` + `DOMAIN-CONTRACTS`
- 修改任务恢复：`TASK-SCHEDULER` + `EVENT-OUTBOX` + `APPROVAL-COMMANDS`
- 部署和发布：`LOCAL-OPERATIONS` + `CONFIG-PROFILES` + `TEST-EVALUATION`
