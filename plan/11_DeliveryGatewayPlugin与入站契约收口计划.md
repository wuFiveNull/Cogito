---
plan_id: "PLAN-11"
title: "Delivery、Gateway、Plugin 与入站契约收口计划"
version: "1.0"
status: "in_progress"
scope: "统一 Delivery 主路径，完成 Gateway 双部署形态，产品化 Plugin Runtime，修复 ContentPart null/default 契约并恢复发布门禁"
depends_on: ["PLAN-10"]
---

# PLAN-11：Delivery、Gateway、Plugin 与入站契约收口计划

## 1. 问题与决策

本计划修复 PLAN-10 组件存在但主路径未收敛的问题。权威设计引用：

- `SYSTEM-BOUNDARIES / 1. 进程边界、4. 状态所有权`
- `DOMAIN-CONTRACTS / 1.12 Delivery、2.5 ContentPart`
- `RUNTIME-FLOWS / 3.10 Exactly Once 的限制、3.11 事务边界`
- `CAPABILITY-PLUGINS / 7. Plugin Package、8. 版本与依赖冲突`
- `TOOL-SANDBOX / 4. Sandbox Profile、7. Shell 与代码`
- `LANGBOT-BRIDGE / 1. 所有权、6. Delivery 操作、10. 通信与版本`

冻结决策：

1. `DeliveryService` 是 Delivery 聚合唯一写入者；主动任务不得拥有第二套实现。
2. Gateway 只执行平台操作并返回安全结果，不创建 Core Delivery。
3. 合并进程和独立进程分别使用 `LoopbackGatewayClient`、`HttpGatewayClient`，共享同一 Port。
4. `PluginRuntime` 是 Port；`SqlitePluginRuntime` 是唯一状态写入者；第三方插件默认由独立宿主进程运行。
5. `ContentPart.size` 的跨进程和数据库规范值为非负整数；未知或内联内容使用 `0`，不使用 `null`。

## 2. M1：Inbound / ContentPart 契约

- `InboundContent.size` 默认 `0`；Bridge 解码和 Dispatcher 边界统一归一化。
- `DOMAIN-CONTRACTS`、`MESSAGE-PERSISTENCE`、`DATABASE-SCHEMA` 明确 required/null/default/serialization 映射。
- Channel 与 QQ E2E 不再出现 `content_parts.size` NOT NULL 失败。

验收：相关 Channel/QQ E2E 全绿。

## 3. M2：Delivery / Gateway / Bridge

- 删除主动推送的重复 Delivery 实现，仅保留兼容重导出。
- `RuntimeApplication` 构造唯一 `SqliteDeliveryService`，DeliveryWorker 和 Task Handler 共享该实例。
- Gateway Port 覆盖 send/placeholder/edit/finish/delete/reconcile/health。
- 实现 Loopback 与 HTTP 两种 Client；HTTP 超时映射为 unknown。
- Bridge 执行真实 Gateway 操作，并按 operation key 持久化幂等 Receipt。
- unknown Delivery 只能调用 Gateway reconcile，不允许盲目 retry。

验收：Delivery、Bridge、HTTP、迁移和应用装配测试全绿。

## 4. M3：Plugin Runtime 产品化

- Manifest 持久化 source/source_path/hash/isolation/trust/permissions。
- 实现 `PluginProcessSupervisor`：参数数组启动、最小环境、ready 握手、健康检查、超时终止和关闭清理。
- 实现 `PluginPolicyAdapter`，启动前把 Manifest 权限映射到显式 Grant；拒绝写 Audit。
- 升级前写 Snapshot，提供 rollback。
- Command API 通过 Plugin Runtime 禁用插件；Query API 查询真实 Plugin 状态。
- 旧测试从实例化 Protocol 迁移到具体实现。

验收：进程崩溃不拖垮 Core、越权拒绝、状态持久化、升级回滚和审计测试全绿。

## 5. M4：架构治理与发布门禁

- 落地 ADR-001/002，统一存放于 `markdown/00_guides/adr/`。
- 更新 manifest、README、依赖图、配置示例和受影响功能规范。
- 修复测试环境硬编码解释器路径；完整测试、Ruff、Web typecheck/build、Playwright smoke 全绿。

## 6. 完成定义

- 仓库只有一个 `class SqliteDeliveryService`。
- `BridgeServer._handle_delivery` 无 TODO/accepted 占位。
- `HttpGatewayClient` 和 `PluginProcessSupervisor` 有真实契约测试。
- Inbound、QQ、Delivery、Plugin、架构与完整测试全绿。
- 文档状态与代码、测试、迁移一致；没有未登记例外。
