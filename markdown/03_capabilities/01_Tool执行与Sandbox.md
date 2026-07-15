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
→ allow | deny | approval | allow_with_constraints
→ Auto Mode
→ Approval（必要时暂停 Turn）
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

Policy 输出携带不可变 `ConstraintSet`：`allowed_paths`、`protected_paths`、
`allowed_hosts`、`network_enabled`、`mount_mode`、`timeout_seconds`、
`max_output_chars`、`max_result_items` 与 `max_write_bytes`。Auto Mode、Approval
和 Runtime 只能求交或缩小该集合；执行前发现请求超界时直接拒绝。

### 3.1 Auto Mode

Auto Mode 是确定性 Policy 之后、执行之前的附加自动放行闸门，不是授权来源，也不能把 `deny` 改成 `allow`。

- 仅本地、`risk_level=low` 且 `side_effect_class=none` 的 Tool 默认走确定性快速路径；MCP Tool 不适用该快速路径；
- 其他调用使用独立模型角色进行两阶段分类：第一阶段快速筛查，命中风险后由第二阶段结合最近一次用户请求复核目标、范围和副作用；
- 发送给分类器的参数必须先做敏感字段脱敏和长度限制，Tool 描述、参数及 MCP 内容都按不可信数据处理；
- 分类器超时、不可用、响应不符合 Schema 或结论不确定时转入人工 Approval；批准后只执行审批时绑定的 capability 版本、规范参数和参数 Hash；
- 配置入口为 `capability.auto_mode`，默认关闭；`safe_tools` 是运维显式维护的确定性例外，不允许由模型动态扩展。

同一模型响应中的多个 Tool Call 必须顺序执行。遇到 Approval 后暂停本 Turn 及其后续 Tool Call，恢复时重新检查确定性 `deny`；Approval 不得覆盖 `deny`。

## 4. Sandbox Profile

```text
read_only
workspace_write
network_restricted
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

`read_file` 与 `grep` 使用验证后的同一文件句柄流式扫描；文件总大小超过
`max_read_bytes` 时仍可通过行号分页读取，而不是先把完整文件载入内存。每页返回
`size_bytes`、`total_lines`、`truncated` 和 `next_offset`，Tool Executor 继续负责将
超过模型输出边界的最终 ToolResult 写入 PayloadStore。

Windows 通过文件句柄解析最终路径并检查 Reparse Point；Linux 通过目录 FD、
`openat` 和 `O_NOFOLLOW` 打开。统一 diff Patch 在提交前完成所有文件的路径、
上下文和 Hash 预检，先生成完整变更计划，再原子提交；失败时回滚已提交文件。

## 6. 网络

- DNS 解析后检查目标 IP，阻止 loopback、元数据和未授权内网；
- 每次重定向重新校验 Host/IP；
- Host Allowlist 可限制端口和协议；
- 限制请求数、响应大小、连接时间和总流量；
- HTTP 响应保持 `external_untrusted`。

内置 `web_fetch` 按上述规则逐次校验重定向。当前落地版 Remote MCP Client 不自动
跟随重定向（`follow_redirects=false`）；需要重定向的 MCP 地址必须直接配置最终
Endpoint。这样在尚未实现连接级 IP 固定前保持 fail-closed。

## 7. Shell 与代码

Cogito 不注册 Shell、后台进程或任意代码执行 Tool，也不提供宿主 Shell fallback。
代码修改只通过 Workspace 文件 Tool 和 command-free unified diff 完成。若插件自行提供
进程执行能力，它属于插件的独立信任边界，不能复用或扩大 Cogito Workspace 权限。

## 8. MCP

MCP Server 在独立进程或受限连接中运行。Server 提供的 Tool Schema 进入 Registry 前校验命名、大小和权限；采样、资源读取和 Roots 能力分别授权，MCP 返回内容不能作为系统指令。

当前实现支持 stdio、SSE 与 Streamable HTTP。stdio 只允许显式配置为
`host_trusted` 的 Server，并且只接收 PATH 与配置白名单中的环境变量；不可信 stdio
Server 必须改用受控的 Remote MCP 部署，否则拒绝启动。Resources 与 Prompts 仅经
`mcp_list_resources`、`mcp_read_resource`、`mcp_list_prompts`、`mcp_get_prompt`
元 Tool 访问，输出统一标记 `external_untrusted`。Capability ID 固定为
`mcp:<server>:<native_tool>`；列表变化经 500ms debounce 生成新的 Provider Snapshot，
既有 Attempt 继续使用原快照。连续失败触发退避和熔断，OAuth 失效进入
`auth_required`，不会无限重试。

Sampling 使用 `Server + Agent Attempt` 作为预算作用域；非 Agent Connector 调用使用
独立 `connector` scope。每个 scope 最多 4 次、累计 8192 Token、墙钟 120 秒，单次
最多 2048 输出 Token，且请求中不提供 Tool。

## 8.1 延迟 Tool 暴露

`tool_search` 与 `tool_describe` 常驻。其余标记为 deferred 的 Tool 不进入每轮模型 Schema；搜索激活结果写入 Attempt checkpoint 的 `ToolExposureState`，CapabilitySnapshot 本身保持不可变。

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

规范参数始终使用稳定 JSON 写入 PayloadStore，数据库仅保存 64 位 SHA-256、
引用与脱敏摘要。所有非 `none` 副作用均写 Receipt；`unknown/pending` 会创建
`tool.reconcile` Task，普通 Retry 不得重放原动作。

## 10. 输出

ToolResult 通过输出 Schema、大小限制、Trust Label 和敏感信息扫描。结构化结果按完整
JSON Schema 校验；声明为 `string`（或包含 `string` 联合类型）的 Tool 可合法返回普通文本，
因此文本型 MCP 结果不会被误判为 JSON 失败。大型结果写 Payload；给模型的内容使用裁剪
摘要和引用，不能把无限输出塞入 Context。

每个 Tool 必须显式声明 `side_effect_class=none|idempotent|reconcilable|non_retriable`。
`reconcilable` 必须提供对账处理器；未知 MCP 动作和外部消息默认 `non_retriable`，禁止自动
重放。`cogito tools audit --all` 用于在启动前检查 Schema、Trust Label、审批策略和副作用
契约是否完整。

## 11. 配置与指标

配置包括 Profile、Scope、默认超时、允许 Host/Path 和审批策略。指标包括执行数、拒绝率、审批率、超时、unknown、输出超限、MCP 重连与熔断。

## 12. 测试

覆盖路径穿越、符号链接逃逸、DNS Rebinding、重定向、Secret 泄漏、超大输出、取消竞态、超时后对账和重复幂等键，并断言 Registry 不包含 Shell/进程 Tool。

