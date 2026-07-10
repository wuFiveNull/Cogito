---
doc_id: "ADR-001"
title: "Runtime 与 Application 组合边界"
status: "accepted"
date: "2026-07-10"
---

# ADR-001：Runtime 与 Application 组合边界

## 背景

Delivery、主动任务、Plugin 和 Channel 曾分别构造具体实现，造成重复状态写入口和组件测试通过但主路径未使用的问题。

## 决策

`RuntimeApplication` 是唯一组合根，负责创建并持有：

- 一个 `SqliteDeliveryService`；
- 一个选定的 `GatewayClient`；
- 一个 `SqlitePluginRuntime`；
- Channel、Worker、Scheduler 和 MCP 生命周期。

业务模块只依赖 Port。主动任务、Web Command 和 Worker 通过组合根注入的实例请求状态变化，不自行构造第二实现。

## 后果

合并进程仍保留逻辑边界；关闭顺序由组合根统一管理。维护或 enqueue-only 场景可以使用安全的 unavailable Gateway，但不能发送真实消息。

## 迁移与回滚

主动投递模块只保留 `SqliteDeliveryService` 兼容重导出。回滚时可切回旧调用点，但不得同时启用两个写入实现。

## 验证方式

架构扫描、应用启动测试、Delivery/Plugin 唯一写入口测试和完整测试套件。
