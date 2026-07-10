---
doc_id: "LANGBOT-BRIDGE"
title: "LangBot Bridge 契约"
version: "1.0"
status: "active"
source_of_truth: true
layer: "functional-spec"
domain: "interaction"
authority: "channel-bridge"
scope: "LangBot 与 Agent Core 的入站、身份引用、Reply Route、Delivery 和版本契约"
tags: ["langbot", "channel", "bridge"]
depends_on: ["ACCESS-DELIVERY", "DOMAIN-CONTRACTS"]
related_docs: ["MESSAGE-PERSISTENCE", "STREAMING-DELIVERY", "SYSTEM-BOUNDARIES"]
language: "zh-CN"
---

# LangBot Bridge 契约

## 1. 所有权

LangBot 拥有平台连接、SDK 对象、平台身份解析、消息格式转换和发送能力。Agent Core 拥有 Principal 映射、Conversation/Session、Message、Turn 和 Delivery 状态。双方只交换版本化 DTO。

## 2. 入站 ChannelEnvelope

```text
schema_version
event_id/message_id
channel_type/instance_id
platform_conversation_id/thread_id
platform_sender_id
sender_endpoint_ref
conversation_endpoint_ref
platform_message_id
content_parts
reply_route
received_at/platform_timestamp
trust_label
capability_snapshot
raw_payload
trace_context
```

Endpoint Ref 是 LangBot Adapter 提供的稳定、不透明字符串；Core 不解释内部结构。

## 3. 身份边界

LangBot 负责区分群、成员、Bot、Thread 和投递目标。Core 根据稳定 Ref 绑定 Principal。显示名称、头像和昵称只存 Metadata；它们不能作为身份键。

## 4. Reply Route

```text
channel_instance_id
platform_conversation_id
thread_id
reply_to_platform_message_id
reply_token
reply_token_expires_at
target_endpoint_ref
```

Reply Route 创建后作为快照。Token 过期返回 `route_expired`；Core 根据 Delivery Policy 选择安全 fallback 或失败，不让 Gateway 猜测新目标。

## 5. 入站幂等

首选键为 `channel_instance_id + platform_event_id`，消息事件可退化为 platform_message_id。Bridge 重试必须复用 event ID；无稳定 ID 时标记 best-effort 并提供内容 Hash/时间窗口。

## 6. Delivery 操作

```text
send
start_placeholder
append_or_replace
finish
delete
reconcile
```

每次请求包含 delivery_id、attempt_id、operation_seq、幂等键、目标快照和内容。返回平台 Message ID、状态、Receipt、限流和安全错误。Gateway 不直接接收模型 Delta。

Core 的 `DeliveryService` 在调用前持久化发送意图；Bridge 只调用
`GatewayClient` 执行平台操作，不创建或修改 Core Delivery。Gateway 按
`idempotency_key`（缺失时退化为 delivery/attempt/operation_seq/action）保存
操作 Receipt，重复请求返回原结果。合并进程使用 Loopback 实现，独立进程
使用 HTTP 实现，两者共享同一操作集合。

## 7. Capability Snapshot

```text
supports_edit/stream/buttons/files/threads/delete
max_message_length/max_file_size
reply_token_ttl
rate_limit_hint
```

Core 在创建 Delivery 时保存 Snapshot；能力变化可导致降级，但不能使已批准目标发生变化。

## 8. 附件

入站附件先由 Gateway 进行大小、类型和平台权限检查，再传受限下载引用或已落盘 Payload。Core 不接收可无限期使用的平台 Secret URL。出站附件使用 Payload Manifest 和明确文件名，不传宿主任意路径。

## 9. 错误

统一错误：`auth_error | rate_limited | route_expired | unsupported | too_large | temporary | permanent | unknown_result`。未知发送结果进入 Delivery reconcile。

## 10. 通信与版本

默认 loopback HTTP/Unix Socket。Schema 当前版和前一兼容版并存；未知必填字段语义或不支持版本返回明确错误。健康接口报告每个 Channel Instance 的连接、认证、限流和最后事件时间。

配置：`channel.gateway_url` 为空时使用合并进程 Loopback；非空时 Core 使用
`HttpGatewayClient`。HTTP timeout/响应丢失统一映射为 `unknown_result`，不得
自动转换为 temporary 重试。

可观察性：至少记录 action、operation key hash、delivery/attempt/trace ID、
安全状态、耗时和重试建议；不得记录 Secret、完整平台 Token 或无限期 URL。

## 11. 测试

使用 LangBot 录制 Fixture 验证私聊、群聊、多成员、Thread、引用、编辑、撤回、附件、重复事件、Token 过期、限流、流式恢复和版本兼容。

