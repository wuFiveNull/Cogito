---
doc_id: "ADR-002"
title: "Channel Gateway 进程与协议边界"
status: "accepted"
date: "2026-07-10"
---

# ADR-002：Channel Gateway 进程与协议边界

## 背景

Bridge 的出站接口曾只返回 accepted，且计划一度要求 Bridge 反向创建 Delivery，混淆了 Core 状态所有权与平台连接所有权。

## 决策

Core 拥有 Delivery 聚合；Gateway 拥有平台连接和 Gateway operation receipt。调用顺序为：

```text
Core 持久化 Delivery intent
→ GatewayClient 执行版本化平台操作
→ Gateway 返回 platform result
→ Core 持久化 Delivery Receipt/状态
```

合并部署使用 `LoopbackGatewayClient`，独立部署使用 `HttpGatewayClient`。二者实现同一 send/placeholder/edit/finish/delete/reconcile/health Port。Bridge 按 operation key 持久去重，不创建 Core Delivery。

## 错误语义

响应丢失或超时返回 `unknown`，Core 进入 reconcile；不得盲目重试。认证、路由、大小和不支持错误属于永久失败；限流和临时连接错误可调度重试。

## 迁移与回滚

默认保留 loopback 部署。设置 `channel.gateway_url` 后切换 HTTP；失败时清空该配置即可回滚，不改变 Delivery Schema 和状态所有权。

## 验证方式

Bridge V0/V1、operation 幂等、HTTP timeout、Loopback、Gateway health、Delivery unknown/reconcile 和重启恢复测试。
