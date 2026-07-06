---
doc_id: "CONFIG-PROFILES"
title: "配置与 Profile"
version: "1.1"
status: "active"
source_of_truth: true
layer: "implementation-spec"
domain: "infrastructure"
authority: "configuration"
scope: "配置 Schema、覆盖层级、Profile、Secret 引用、热更新和版本"
tags: ["configuration", "profile"]
depends_on: ["SYSTEM-BOUNDARIES"]
related_docs: ["LOCAL-OPERATIONS", "TOOL-SANDBOX"]
language: "zh-CN"
---

# 配置与 Profile

## 1. 配置层级

```text
built-in defaults
→ profile file
→ local override
→ environment override
→ validated runtime override
```

后层只覆盖明确字段。未知字段默认报错，避免拼写错误静默失效。

## 2. 顶层 Schema

```text
runtime/storage/interaction/channel
conversation/agent/model/memory
capability/sandbox/worker/scheduler
connector/proactive/security
observability/retention/backup
```

每个配置项定义类型、默认值、范围、是否敏感、是否可热更新和影响模块。

## 3. Profile

```text
personal      正常本机运行
development   明文 Payload、详细 Trace、Stub 可用
test          临时目录、固定时钟、禁止真实副作用
recovery      只读检查和人工恢复命令
```

Profile 不能通过名称隐式放宽 Tool 权限；权限仍由 Policy 配置明确表达。

## 4. 当前默认值

```yaml
runtime:
  profile: personal
  timezone: Asia/Shanghai
interaction:
  bind_host: 127.0.0.1
  allow_remote: false
  validate_origin: true
storage:
  database: data/database/agent.db
  payload_root: data/payload
  payload_encryption: none
conversation:
  session_policy: channel_conversation
  group_sessions_per_user: true
  thread_sessions_per_user: false
  per_context_partition_concurrency: 1
```

当前 personal Profile 仍按项目阶段使用明文 Payload，以便检查 Agent 行为；development 额外启用更详细 Trace 和 Stub。明文策略不代表未来远程或共享部署默认值。

## 5. Secret

配置只保存 `secret_ref`，由 OS Keyring、环境或受限 Secret Store 解析。配置 dump、Dashboard 和 Trace 显示引用名，不显示值。Secret 更换不需要修改普通业务配置版本。

## 6. 校验

启动时校验路径、端口、时区、Provider、模型能力、插件配置、预算关系和权限范围。跨字段约束包括：远程绑定必须启用认证/TLS；Worker Lease 必须大于 Heartbeat；模型输出预算小于 Context Window。

## 7. 配置版本

有效配置规范化后计算 Hash 并保存：

```text
config_version
schema_version
content_hash
source_layers
activated_at
audit_id
```

RunAttempt、TaskAttempt 和策略决策记录使用的配置版本。

## 8. 热更新

可热更新：预算、冷却、日志级别、普通路由权重。需重启：数据库路径、Payload Root、进程端口、Sandbox 基础镜像。影响正在执行动作的权限变更只收紧当前执行；放宽从下一 Attempt 生效。

## 9. 错误和回滚

新配置先解析、校验和 dry-run，再原子激活。失败继续使用旧版本。配置 Command 支持回滚到历史有效版本并产生 Audit。

## 10. 测试

覆盖未知字段、错误类型、覆盖优先级、Secret 脱敏、跨字段约束、热更新竞态、回滚和 Profile 目录隔离。
