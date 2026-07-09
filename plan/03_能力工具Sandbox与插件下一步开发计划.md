# 03 能力、Tool、Sandbox、Skill、MCP 与插件下一步开发计划

> 状态：draft  
> 对应目录：`markdown/03_capabilities`  
> 设计依据：`CAPABILITY-PLUGINS`、`TOOL-SANDBOX`  
> 关联约束：`DOMAIN-CONTRACTS`、`GLOBAL-INVARIANTS`、`AGENT-LOOP`、`APPROVAL-COMMANDS`、`SECURITY-OBS`

## 1. 目标

将现有 Tool Registry、Tool Executor 和 MCP Client 从“可以调用”提升为“按模式最小暴露、确定性授权、可审计副作用、受限执行、故障隔离、可安装扩展”。

完成后应满足：

1. 新能力按 Footprint Ladder 选择最小核心足迹；
2. Agent 只看到当前模式、Principal 和 Policy 允许的 Tool；
3. 所有副作用 Tool 执行前持久化意图，执行后保存 Receipt；
4. unknown 状态必须 reconcile，不能盲目重试；
5. 文件、网络、Shell、MCP 和第三方插件在声明的 Sandbox Profile 内执行；
6. Skill/Plugin 有发现、验证、启停、健康、降级和回滚生命周期；
7. 第三方插件崩溃不会拖垮 Core。

设计引用：

- `CAPABILITY-PLUGINS / 1. Footprint Ladder`
- `CAPABILITY-PLUGINS / 3. Toolset`
- `CAPABILITY-PLUGINS / 4. Capability Registry`
- `CAPABILITY-PLUGINS / 5. Skill`
- `CAPABILITY-PLUGINS / 7. Plugin Package`
- `TOOL-SANDBOX / 1. 执行链`
- `TOOL-SANDBOX / 4. Sandbox Profile`
- `TOOL-SANDBOX / 9. Receipt 与对账`

## 2. 当前基线

### 已有

- 内置 Tool Registry 和 Executor；
- mode → toolset 的基础映射；
- echo、now、remember/recall/forget 等内置工具；
- Tool Policy 基础 allow/deny/approval 结果；
- ToolCall Repository；
- MCP stdio/SSE Client 与 Manager；
- MCP Connector WIP；
- Tool/Registry/Policy/Executor 和部分 MCP 测试。

### 缺口

- Registry 元数据未完整覆盖版本、权限、风险、副作用、健康和资源需求；
- 内置 Tool 自动发现与冲突策略未形成强门禁；
- ToolCall 状态、Policy 决策、Receipt 和 Reconcile 未形成统一事务链；
- Sandbox 目前更像配置概念，没有完整文件/网络/进程隔离实现；
- Skill 解析、激活、使用记录和归档未实现；
- Plugin Manifest、多源发现、API 版本和隔离生命周期未实现；
- MCP Schema、返回大小、Roots/Sampling/Resource 权限需要细化；
- 插件/Tool 观测和熔断不足。

## 3. 实施原则

1. **先安全闭环，后扩展生态。** Tool Intent/Policy/Receipt 未完成前不开放第三方 Plugin；
2. **最小暴露。** 不因安装插件就把所有 Tool Schema 放入每次模型请求；
3. **权限只缩不扩。** Tool、Skill、Plugin、运行 Profile 多层约束取交集；
4. **外部内容不可信。** MCP/网络/插件输出固定携带 external_untrusted；
5. **第三方默认进程外。** 只有内置或用户显式信任才允许 in_process_trusted；
6. **Core 拥有控制流。** Plugin 只能注册能力，不能新增隐式 Pipeline 或状态机。

## 4. 里程碑

### M1：Capability Registry 2.0

#### Registry 记录

```text
capability_id = namespace:name
kind
version
owner/provider/plugin
toolsets
supported_modes
input/output_schema
permissions/scopes
risk_level
side_effect_class
resource_requirements
availability_check
health
deprecated/disabled
```

#### 工作项

1. 统一内置、Plugin、MCP Tool 的注册入口；
2. Tool 名称使用全局唯一 namespace，Provider 名称映射可逆；
3. 启动阶段完成 Schema 大小、字段、权限和冲突校验；
4. 运行时按 Principal、mode、enabled toolset、Policy 和健康状态过滤；
5. 实现 AST/模块自动发现，单个工具导入失败不静默；
6. 注册结果形成不可变 CapabilitySnapshot，写入 Attempt；
7. 提供只读 Query API 展示来源、版本、状态和不可用原因。

#### 验收

- 新增 `tools/*.py` 后无需手工列表即可注册；
- 同名冲突启动失败并指出来源；
- proactive 模式看不到 terminal/code_exec；
- disabled/deprecated 工具不会进入 Model Schema；
- Tool Schema 顺序稳定，便于缓存和回放。

### M2：Tool 执行事务链

#### 目标链路

```text
ToolRequest
→ resolve/version pin
→ input schema validate
→ deterministic Policy
→ persist proposed/approved intent
→ allocate budget + sandbox
→ execute outside DB transaction
→ output validate/redact
→ persist result/receipt
→ return ToolResult
```

#### 工作项

1. ToolCall 字段对齐 `TOOL-SANDBOX / 2. ToolCall`；
2. canonical arguments 使用稳定序列化并计算 hash；
3. 副作用幂等键由逻辑操作生成，重试复用，禁止纯随机键；
4. PolicyDecision 持久化输入摘要、规则版本、约束和审批引用；
5. `allow_with_constraints` 只能缩小 Path、Host、时长、输出和数量；
6. 执行前提交 approved 意图，网络/进程调用不在事务内；
7. 输出通过 Schema、大小、Trust Label、Secret/PII 扫描；
8. 大型输出写 Payload，模型只接收裁剪摘要和引用；
9. 取消、超时和进程崩溃明确映射 cancelled/failed/unknown。

### M3：Receipt 与 Reconcile

#### 工作项

1. 增加 SideEffectReceipt：external operation id、request hash、状态、摘要、raw ref、reconcile status；
2. Tool Manifest 声明 side_effect_class：none、idempotent、reconcilable、non_retriable；
3. reconcilable Tool 必须实现 `reconcile()`；
4. 超时但无法确认结果时进入 unknown；
5. Recovery 发现 unknown 后只允许：
   - 用 operation id 查询；
   - 使用相同 idempotency key 请求平台；
   - 创建人工处理 Approval；
6. Reconcile 结果和人工决定写 Audit；
7. 回放模式使用 Stub Receipt，不产生真实副作用。

#### 故障测试

- 外部成功、本地 Receipt 提交前崩溃；
- 请求超时但外部已成功；
- 重复 idempotency key；
- Receipt hash 与请求不一致；
- 旧 Attempt 尝试提交 ToolResult。

### M4：Sandbox Runtime

#### Profile

实现：read_only、workspace_write、network_restricted、shell_isolated、plugin_process。

每个 Profile 固定：

- 工作目录和允许 Root；
- 环境变量白名单；
- Secret 注入方式；
- Host/IP/端口/协议；
- CPU、内存、进程数、磁盘、输出、超时；
- 取消和进程树清理；
- 临时目录生命周期。

#### 文件系统

1. `resolve()` 后再校验允许 Root；
2. 打开前复查符号链接目标，防止 TOCTOU；
3. 写入使用临时文件 + 原子替换；
4. 删除、覆盖、递归移动进入高风险 Policy；
5. 返回受控 FileManifest，不暴露任意宿主句柄；
6. 限制单文件、总文件数和总字节。

#### 网络

1. DNS 解析后校验 IP；
2. 阻止 loopback、云元数据、未授权内网；
3. 每次重定向重新验证 Host/IP/协议；
4. Allowlist 支持 host + port + scheme；
5. 限制请求数、连接时间、响应大小和总流量；
6. 响应始终 external_untrusted。

#### Shell/进程

1. 默认参数数组执行，不拼接隐式 Shell；
2. Shell 语法升级风险级别并要求专用 Profile；
3. 默认不继承用户全局环境；
4. 取消先终止完整子进程树，再清理 Sandbox；
5. 记录安全参数摘要、cwd、exit code、资源和 output_ref。

### M5：MCP 安全接入

#### 工作项

1. Server 启动时拉取并校验 Tool Schema、版本、大小和命名；
2. 每个 MCP Server 配置 allowed_tools、toolset、Roots、Sampling、Resources 和返回上限；
3. stdio Server 使用 plugin_process/sandbox Profile；
4. SSE/HTTP Server 应用网络 allowlist 和超时；
5. MCP 返回内容固定 external_untrusted，不能注入 system prompt；
6. Server 不可用仅降级相关能力；
7. Schema 变化生成新 CapabilitySnapshot，不修改正在执行 Attempt；
8. 修复测试中的 Python 解释器硬编码，使用当前环境可执行文件；
9. 增加超大返回、恶意 Schema、断连、超时和重连测试。

### M6：Skill Runtime

#### 工作项

1. 实现 SKILL.md frontmatter/Markdown 完整解析和错误定位；
2. 支持内置、用户、Plugin Skill 来源及同名覆盖规则；
3. Skill 声明的 toolsets/permissions 不能超过 Plugin Manifest；
4. 激活方式：被动匹配、显式 `/skill-name`、skill_loader；
5. Skill 内容作为不可信扩展指令经过 Prompt Policy；
6. 记录 loaded_at、last_used_at、use_count、来源版本；
7. 状态 active/stale/archived/pinned；
8. 归档只移动到 `.archive`，支持恢复，不自动删除；
9. Agent 自创 Skill 只能先生成草稿，经用户确认后写入；
10. Skill 不获得超出当前 Agent/Plugin 的权限。

#### 测试

- 所有 frontmatter 字段和边界；
- 路径穿越、符号链接和超大 Skill；
- 同名覆盖；
- stale/archived/pinned 时间边界；
- 被动激活误匹配；
- Skill 请求未授权 Toolset。

### M7：Plugin Runtime

#### 工作项

1. 多源发现：内置、用户、项目（显式开关）、pip entry-point；
2. Manifest 校验 plugin_id、版本、api_version、权限、隔离、配置 Schema、依赖；
3. 生命周期：discovered → validated → installed → configured → enabled → started → running；
4. 支持 degraded/disabled/stopped/uninstalled；
5. 第三方默认 subprocess，只有显式信任才 in_process_trusted；
6. PluginContext 只暴露公开 API、声明权限和注册方法；
7. Core 私有模块、数据库和 Secret 默认不可见；
8. 单次失败标准化；连续 3 次/60 秒触发熔断；
9. 启动失败只禁用该插件，required 插件例外；
10. 升级前保存配置/状态快照，失败自动回滚；
11. 卸载前检查正在运行的 Task/Tool/Connector 引用。

#### 隔离验收

- Plugin import error 不阻止 Core 启动；
- 子进程崩溃可检测和重启/禁用；
- 超预算被终止且无残留进程；
- 未声明网络/文件权限调用被拒绝并 Audit；
- API 版本不兼容拒绝启动；
- Plugin 无法直接修改 Turn/Memory/Delivery 表。

### M8：观测、控制面与治理

#### 指标

- Tool 执行/拒绝/审批/unknown；
- Sandbox 启动和清理耗时；
- 资源超限和残留进程；
- MCP 健康、重连和 Schema 变化；
- Skill active/stale/archived 使用量；
- Plugin 启停、故障和熔断。

#### 控制命令

- enable/disable plugin；
- restart/degrade plugin；
- enable/disable capability；
- inspect capability snapshot；
- reconcile tool call；
- restore archived skill。

所有写操作必须使用 Command API、expected_version 和 Audit。

## 5. Schema 与配置

建议新增/扩展：

- capabilities / capability_snapshots；
- tool_calls / tool_receipts / policy_decisions；
- skills / skill_usage；
- plugins / plugin_versions / plugin_health；
- sandbox_profiles；
- MCP Server 细粒度权限。

配置 Secret 仅使用引用，不把值写入 Plugin Manifest、Trace 或 Dashboard。

## 6. 测试与发布门禁

必须覆盖 `TOOL-SANDBOX / 12. 测试`：路径穿越、符号链接、DNS Rebinding、重定向、Secret、进程树、超大输出、取消竞态、unknown 对账和重复幂等键。

发布前还需：

1. 内置 Tool 自动发现全通过；
2. mode/toolset 分发全通过；
3. MCP 恶意 Fixture 全通过；
4. Plugin subprocess 崩溃不影响 Core；
5. 回放模式零真实副作用；
6. Windows/Linux 至少各完成一次 Sandbox 演练。

## 7. 完成定义

1. Tool 执行全链有 Intent、Policy、Budget、Result/Receipt；
2. unknown 不会自动重复副作用；
3. 五类 Sandbox Profile 有可执行约束和逃逸测试；
4. MCP 的 Schema、权限、Trust Label 和返回大小受控；
5. Skill 生命周期和 Plugin 生命周期实现并可审计；
6. 第三方 Plugin 默认进程外，崩溃不影响 Core；
7. 全套测试、Ruff、compileall 通过。

## 8. 建议拆分 PR

1. PR-C1：Registry 2.0 与 Toolset Snapshot；
2. PR-C2：Tool Intent/Policy/Receipt/Reconcile；
3. PR-C3：文件和进程 Sandbox；
4. PR-C4：网络 Sandbox 与安全 Fixture；
5. PR-C5：MCP 权限、Schema 和故障隔离；
6. PR-C6：Skill parser/activation/lifecycle；
7. PR-C7：Plugin discovery/manifest/subprocess；
8. PR-C8：控制面、指标、熔断和升级回滚。
