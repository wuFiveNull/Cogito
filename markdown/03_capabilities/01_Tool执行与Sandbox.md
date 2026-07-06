---
doc_id: "TOOL-SANDBOX"
title: "Tool 执行与 Sandbox"
version: "1.0"
status: "active"
source_of_truth: true
layer: "functional-spec"
domain: "capabilities"
authority: "tool-execution"
scope: "Tool 请求、Policy、Sandbox、Receipt、对账和故障处理"
tags: ["tool", "sandbox", "policy", "receipt"]
depends_on: ["CAPABILITY-PLUGINS", "APPROVAL-COMMANDS"]
related_docs: ["AGENT-LOOP", "GLOBAL-INVARIANTS"]
language: "zh-CN"
---

# Tool 执行与 Sandbox

## 1. 执行链

```text
ToolRequest
→ Registry resolve
→ input schema
→ Policy evaluation
→ allow | deny | approval
→ persist ToolCall intent
→ allocate budget/sandbox
→ execute
→ validate output
→ persist Receipt/Result
→ return ToolResult
```

Agent Runtime 不直接持有 Executor、Secret 或系统句柄。

## 2. ToolCall

```text
tool_call_id
attempt_id or task_attempt_id
tool_id/version
canonical_arguments_ref
arguments_hash
idempotency_key
risk_level
requested_scopes
policy_decision_id
status: proposed|approved|executing|succeeded|failed|unknown|cancelled
timeout_at
version
```

副作用 Tool 在调用外部系统前必须已提交 `approved` 状态和稳定幂等键。

## 3. Policy

Policy 输入包含 Principal、运行模式、Trust Label、目标资源、参数、权限、风险、预算和现有 Approval。`allow_with_constraints` 可以缩小路径、Host、时长、输出大小和操作数量，不能扩大 Manifest 权限。

## 4. Sandbox Profile

```text
read_only
workspace_write
network_restricted
shell_isolated
plugin_process
```

Profile 明确工作目录、挂载、环境变量、网络、CPU、内存、进程数、磁盘、超时和清理策略。默认无 Secret、无 Core 数据库访问、无继承用户全局环境。

## 5. 文件系统

- 先解析真实路径再匹配允许 Root；
- 每次打开前再次检查符号链接目标；
- 写入临时文件并原子替换；
- 删除、覆盖和批量移动使用高风险策略；
- 返回文件 Manifest，不返回任意宿主路径句柄；
- 限制单文件和总输出大小。

## 6. 网络

- DNS 解析后检查目标 IP，阻止 loopback、元数据和未授权内网；
- 每次重定向重新校验 Host/IP；
- Host Allowlist 可限制端口和协议；
- 限制请求数、响应大小、连接时间和总流量；
- HTTP 响应保持 `external_untrusted`。

## 7. Shell 与代码

命令使用参数数组，不通过隐式 Shell 拼接。确需 Shell 语法时使用更高风险 Profile。执行记录程序、参数安全摘要、工作目录、退出码、资源消耗和输出引用；取消先终止子进程树，再清理 Sandbox。

## 8. MCP

MCP Server 在独立进程或受限连接中运行。Server 提供的 Tool Schema 进入 Registry 前校验命名、大小和权限；采样、资源读取和 Roots 能力分别授权，MCP 返回内容不能作为系统指令。

## 9. Receipt 与对账

```text
receipt_id
tool_call_id
external_operation_id
request_hash
status
result_summary
raw_receipt_ref
reconcile_status
created_at
```

超时且外部结果未知时 ToolCall=`unknown`。恢复必须先使用外部 Operation ID、幂等接口或查询 Tool 执行 `reconcile()`；无法对账时等待人工处理。

## 10. 输出

ToolResult 通过输出 Schema、大小限制、Trust Label 和敏感信息扫描。大型结果写 Payload；给模型的内容使用裁剪摘要和引用，不能把无限输出塞入 Context。

## 11. 配置与指标

配置包括 Profile、Scope、资源限制、默认超时、允许 Host/Path 和审批策略。指标包括执行数、拒绝率、审批率、超时、unknown、Sandbox 启动时间、资源超限和残留进程。

## 12. 测试

覆盖路径穿越、符号链接逃逸、DNS Rebinding、重定向、Secret 泄漏、进程树清理、超大输出、取消竞态、超时后对账和重复幂等键。

