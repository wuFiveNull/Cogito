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
minimal       最小 reactive Agent，仅 core/memory，Auto Mode 关闭
developer     显式 Workspace Root，启用常用 Toolset 和 Auto Mode
personal      不注册文件/Web Tool，启用个人 Skill、Schedule、子 Agent 与主动能力
```

Profile 不能通过名称隐式放宽 Tool 权限；权限仍由 Policy 配置明确表达。
三套模板均不预装或配置网页搜索 MCP；需要时必须单独增加可信 Server 配置。

使用 `cogito config profiles` 枚举内置模板；使用
`cogito config init --profile developer --output config.toml` 原子生成配置。目标已存在时默认
拒绝覆盖，只有显式 `--force` 才替换；写入前会使用当前 Config Schema 完整校验。

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
channel:
  gateway_url: ""              # 空=Loopback；非空=独立 Gateway HTTP
capability:
  plugins:
    enabled: false
    auto_start: false
    builtin_paths: []
    project_paths: []
    granted_permissions: []     # Manifest 权限必须是该集合的子集
```

当前模板用于本地先落地：模型 Provider 默认为 `echo`，用户应在真实运行前替换；developer
的 Workspace Root 为当前目录且保护 `.git/.env/.workspace/.venv/config.toml`，personal
未配置 Workspace Root，因此不会注册文件 Tool。

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
