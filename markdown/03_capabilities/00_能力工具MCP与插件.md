---
doc_id: "CAPABILITY-PLUGINS"
title: "能力、工具、MCP 与插件"
version: "2.0"
status: "active"
source_of_truth: true
layer: "subsystem-overview"
domain: "capabilities"
authority: "capability-boundaries"
scope: "Footprint Ladder、Provider、Middleware、Toolset、Skill（SKILL.md）、MCP、自动发现、多源发现、Plugin Runtime"
tags:
  - "capability"
  - "tool"
  - "skill"
  - "mcp"
  - "plugin"
  - "provider"
  - "middleware"
  - "toolset"
  - "discovery"
depends_on:
  - "ARCH-OVERVIEW"
  - "DOMAIN-CONTRACTS"
  - "RUNTIME-FLOWS"
related_docs:
  - "AGENT-COGNITION"
  - "SECURITY-OBS"
  - "OPS-GOVERNANCE"
  - "TOOL-SANDBOX"
language: "zh-CN"
---

# 能力、工具、MCP 与插件

> **文档编号**：CAPABILITY-PLUGINS  
> **适用范围**：Provider、Middleware、Toolset、Tool、Skill、MCP、Capability Registry、插件发现与 Plugin Runtime  
> **权威边界**：本文是其范围内的规范性来源；总体架构文档只负责概念与边界。  
> **细化规范**：Tool 请求、Policy、Sandbox、Receipt 和对账以 `TOOL-SANDBOX` 为准。  
> **关联文档**：AGENT-COGNITION, SECURITY-OBS, OPS-GOVERNANCE

## 阅读说明

**目的**：定义系统可扩展能力的类型、分组、注册、执行、Skill 文件格式、插件发现、版本和隔离规则。

**边界**：Plugin Package 只是生命周期和权限载体，不拥有独立控制流。

**建议读取方式**：新增 Tool、Skill、MCP、Provider 或第三方插件时读取。

## 1. Footprint Ladder：选择能力引入层级

新增能力时，按以下阶梯选择**最高（最少核心足迹）**的可行层级：

```text
1. 扩展现有 Tool/Skill — 能力是已有能力的变体，零新接口
2. CLI 命令 + Skill — 能力可通过 shell 命令配置/调用，零 tool schema 增长
3. Service-gated Tool — 能力仅在特定服务/配置就绪时可见，零常态足迹
4. Plugin — 第三方/小众/用户专属能力，通过 ~/.cogito/plugins/ 安装
5. MCP Server — 结构化 I/O 但非核心基础能力，外部 Tool 协议接入
6. 新核心 Tool — 基础、通用、几乎所有用户都需要，且无法通过终端+文件达成
```

每降一级，永久 schema 足迹增加。核心 Tool 的 bar 最高——它发送在每一次 API 调用上。

## 2. 扩展、插件与能力架构

### 2.1 扩展目标

扩展体系用于：

- 替换模型、Embedding、检索器和 Channel 驱动；
- 注册 Tool、Skill、Connector 和 JobHandler；
- 在固定调用边界执行策略、记录和转换；
- 安装独立功能包；
- 在不修改 Core 领域规则的前提下增加能力。

扩展体系不用于让任意插件重写系统控制流。

### 2.2 Provider

Provider 替换一个完整能力实现：

```text
ModelProvider
EmbeddingProvider
MemoryRetriever
MemoryRanker
ChannelDriver
PayloadStore
TraceExporter
NotificationRenderer
ClockProvider
```

Provider 声明：

```text
provider_id
interface_version
capabilities
config_schema
lifecycle
concurrency_safety
health_check
fallback_behavior
```

同一 Slot 同时只有一个 Active Provider，除非该 Slot 明确支持组合。Provider 失败切换由 Core Router 决定，不由插件互相调用。

### 2.3 Middleware

Middleware 只包裹一个明确调用：

```text
request
→ before
→ next(request)
→ after(result)
```

允许结果：

```text
Continue(modified_request)
Reject(reason)
Return(result)
RequireApproval(request)
```

普通 Middleware 不得：

- 任意创建分支 Pipeline；
- 修改受保护字段；
- 直接提交跨模块数据库状态；
- 无限等待用户；
- 静默吞掉错误；
- 自行提升权限。

### 2.4 固定 Policy Gate

允许修改或拒绝数据的 Gate 仅包括：

```text
input_policy
context_policy
prompt_policy
tool_policy
memory_write_policy
output_policy
delivery_policy
```

其他 Observer Hook 只能读取和记录。不允许插件自行新增 Policy Gate。Observer 位置可扩展——插件可以在固定调用点（Turn 开始/结束、Tool 调用前后、Memory 写入前后）注册只读观察者。

### 2.5 Capability 分类

```text
Tool        原子执行能力
Skill       完成某类任务的方法、提示和编排模板
Connector   外部数据源输入
JobHandler  持久 Task 的执行器
MCP Server  外部 Tool 协议端点
Renderer    Channel 内容渲染能力
UI Extension Dashboard 扩展
```

Tool 与 Connector 必须分开：

```text
Connector  数据进入系统
Tool       系统对外执行或查询
```

---

## 3. Toolset：工具分组与按模式分发

### 3.1 概念

Toolset 是一组 Tool 的逻辑集合。同一 Tool 可属于多个 Toolset。Agent 在不同运行模式下看到不同的 Toolset 子集，避免无关工具占用 schema 空间和 token 预算。

### 3.2 内置 Toolset

```text
core           所有模式均加载的基础工具集（文件读写、消息查询、recall_memory）
terminal       Shell 和命令执行相关
browser        浏览器和网页交互
code_exec      代码执行和沙箱
vision         图像/视频分析
memory         记忆管理（memorize、forget_memory）
message        消息发送和通知
search         网页搜索和抓取
connector      数据源连接器操作
schedule       定时任务和提醒
delegation     子 Agent 派生
disk           文件系统和磁盘操作
```

`delegate_task` 是延迟暴露 Tool，支持 `general`、`researcher`、`coder`、`reviewer`、
`planner` 五种本地角色预设。只读角色在 Capability Snapshot 阶段排除所有有副作用的
Tool；所有角色的 Toolset 都是父 Agent 权限、角色策略和请求 Toolset 的交集。子 Agent
预算只能从父 Attempt 剩余预算中缩小，不能通过参数扩大。（`CAPABILITY-PLUGINS / 4`，
`TASK-SCHEDULER / 8`）

### 3.3 模式-Toolset 映射

```text
reactive（被动回复）       core + terminal + browser + code_exec + vision
                          + memory + search + delegation + disk

proactive（主动推送）      core + memory + search + message

scheduled（定时任务）      core + memory + connector + message + schedule

maintenance（后台维护）    core + memory + disk（只读）

drift（空闲整理）          core + memory + disk（只读）
```

Agent Runtime 在构建请求时根据当前模式组装工具 schema。模式切换不改变已注册的 Tool，只改变当次请求可见的工具列表。

### 3.4 用户配置

用户可通过配置启用/禁用特定 Toolset：

```yaml
tools:
  enabled:
    - core
    - terminal
    - search
    - memory
  disabled:
    - delegation
```

禁用 Toolset 对整个系统的所有模式生效。单个 Tool 可通过 Registry 的 `deprecated` 或 `disabled` 标记下线。

---

## 4. Capability Registry

### 4.1 注册机制

系统提供两种注册路径：

**路径 A — 组合根显式发现（内置 Tool）**：`discover_builtin_tools()` 导入内置
ToolDef 或调用依赖注入工厂，统一注册到 Registry。带外部依赖的 Tool 不在模块
import 时修改全局状态：

```python
# tools/my_tool.py
from tools.registry import registry

registry.register(
    name="my_tool",
    toolset="search",             # 所属 Toolset（可多选，逗号分隔或列表）
    schema={...},                 # OpenAI function calling 格式
    handler=lambda args, **kw: ...,
    check_fn=check_requirements,  # None = 始终可用
    requires_env=["MY_API_KEY"],  # 所需环境变量
)
```

启动时组合根传入 Memory、Knowledge、Workspace、数据库连接和 Capability 配置。
Workspace Root 未配置时文件 Tool 不注册。核心 Registry 不提供 Shell、后台进程或
任意代码执行 Tool；stdio MCP 仅允许显式声明的 `host_trusted` Server。

**路径 B — 插件注册**：插件在 `register(ctx)` 函数中调用 `ctx.register_tool(...)`，底层委托给同一个 Registry。插件 Tool 的 toolset 可在 `plugin.yaml` 中声明默认值，用户可覆盖。

### 4.2 Registry 记录

```text
name
version
toolset               — 所属工具分组
provider/plugin owner
input/output schema
permissions
risk level
side effect classification
supported modes        — 允许的运行模式，空 = 全部
resource requirements
check_fn               — 运行时可用性检查
deprecated             — 废弃但尚未删除
health
```

Agent 只能看到当前 Principal、运行模式和 Policy 允许的 Capability 子集。

---

## 5. Skill

### 5.1 Skill 文件格式（SKILL.md）

Skill 是一个目录，最少包含一个 `SKILL.md` 文件，可选附带 `scripts/`、`references/`、`templates/` 子目录：

```text
skills/my-skill/
├── SKILL.md           — 必需：YAML frontmatter + Markdown 指令
├── scripts/           — 可选：辅助脚本
├── references/        — 可选：参考文档
└── templates/         — 可选：输出模板
```

**SKILL.md 格式**：

```markdown
---
name: my-skill
description: 用一句话描述此 Skill 能完成什么（≤ 60 字符）
version: "1.0"
author: "author-name <email>"
platforms: [linux, macos, windows]
metadata:
  toolsets: [terminal, search]   # 执行此 Skill 需要的 Toolset
  tags: [tag1, tag2]
---

# <Skill 名称> Skill

<2-3 句话说明此 Skill 做什么、不做什么>

## When to Use
- 当用户需要 X 时
- 当上下文出现 Y 时

## Prerequisites
- 需要启用的 Toolset：terminal, search
- 需要的 API Key：XXX（参见 .env.example）

## Procedure
### Step 1: 确定输入
...

### Step 2: 执行核心操作
...

## Pitfalls
- 常见错误 1：...
- 常见错误 2：...

## Verification
- 验证标准：...
```

### 5.2 Skill 存储与加载

```text
内置 Skill     <repo>/skills/<name>/SKILL.md          — 随项目分发
可选 Skill     <repo>/optional-skills/<name>/SKILL.md  — 需用户安装
用户 Skill     capability.skills.root/<name>/SKILL.md  — 显式配置后可管理
插件 Skill     插件包内的 skills/ 目录
```

加载顺序：内置 → 用户（同名覆盖）。Skill 的 toolsets 声明不得超过 Plugin Manifest 的权限。
用户 Skill Root 不从 cwd、HOME 或 Workspace Root 隐式推导；未配置时
`skill_manage` 不注册。创建、更新、归档和恢复通过 Command Service 写 Audit 与
Outbox，更新和状态变化要求 `expected_version`，归档采用软删除。

### 5.3 Skill 激活方式

```text
被动激活    Agent 判断当前任务匹配 activation_hints 时自动加载指令
显式激活    用户输入 /skill-name 或 Agent 在推理中调用 skill_loader 工具
Drift 激活  空闲时 Agent 选择一项 Skill 作为后台维护任务执行
```

### 5.4 Agent 自创 Skill

Agent 在完成复杂任务后，可以提议创建新 Skill（存为 `kind=episode` 的 MemoryItem 草稿 + SKILL.md 模板）。用户确认后，写入 `~/.cogito/skills/`。自创 Skill 在其 toolsets 限制内执行，不能获得超出 Agent 已有的权限。

### 5.5 Skill 生命周期

```text
active     — 正常可用
stale      — 超过 30 天未被加载，降级提示但不删除
archived   — 超过 90 天未使用，移至 .archive/，可恢复
pinned     — 用户标记为永久保留，跳过自动归档
```

后台维护任务（Drift）周期性检查 Skill 使用记录，按规则变更状态。归档不删除目录——用户可手动 `restore`。Pinned Skill 豁免一切自动状态变更。

---

## 6. MCP

每个 MCP Server 作为独立 Capability Provider：

- 配置允许的 Tool 列表；
- 配置网络和文件权限；
- 对返回内容标记 `external_untrusted`；
- 限制返回大小；
- 记录 Server 版本和 Tool Schema；
- Server 不可用时只影响相关 Capability。

MCP Server 提供的 Tool 自动进入 Registry，toolset 默认设为 Server 名称。用户可将其重新分配 Toolset 或禁用单个 Tool。

Registry 对动态 Provider 更新使用并发安全快照。MCP Tool 本地配置决定风险、
审批策略、权限与副作用分类；Server 自报元数据不得扩大权限。未配置副作用分类的
远程 Tool 默认为 `non_retriable`，避免响应丢失后重复外部动作。Manager 支持列表变化通知、
Schema/数量/大小校验、稳定别名、指数退避、熔断和健康状态。Sampling 使用独立
无 Tool 模型角色，并按 `Server + Agent Attempt` 隔离调用次数、Token 与墙钟预算；
Roots 只来自显式 Workspace/roots 配置。当前 Remote MCP 对 HTTP 重定向采用严格
拒绝策略，配置必须指向最终 Endpoint。

本地可通过 `cogito tools list|describe|audit` 查看实际 Registry、完整输入/输出契约和
契约问题，通过
`cogito mcp list|status|tools` 查看配置、健康状态和动态原生 Tool。诊断过程只做
MCP initialize/list/health，不执行模型或副作用 Tool。

本地 Query API 同时提供只读 `/api/tools`、`/api/tools/{name}` 与 `/api/mcp/status`；
它们复用当前 Runtime 的 Registry 和 Manager，不创建第二套 MCP 生命周期。

---

## 7. Plugin Package

### 7.1 多源发现

系统从以下路径按优先级发现插件（同名时高优先级覆盖低优先级）：

```text
1. 内置插件     <repo>/plugins/<name>/plugin.yaml
2. 用户插件     ~/.cogito/plugins/<name>/plugin.yaml
3. 项目插件     ./.cogito/plugins/<name>/plugin.yaml  (需 COGITO_ENABLE_PROJECT_PLUGINS=1)
4. pip 插件     pip 包暴露 cogito_agent.plugins entry-point
```

每个插件目录必须包含 `plugin.yaml` 和 `__init__.py`（含 `register(ctx)` 函数）。

### 7.2 Manifest

```yaml
# plugin.yaml
plugin_id: my-plugin
name: My Plugin
version: "1.0.0"
api_version: ">=1.0,<2.0"
description: 一句话描述
author: author-name
entry_points:
  register: my_plugin.register
permissions:
  - filesystem:read
  - network:outbound
isolation: subprocess           # in_process_trusted | subprocess | sandbox | remote_mcp
config_schema: {}
dependencies: {}
```

### 7.3 Plugin 生命周期

```text
discovered
→ validated（API 版本、权限、Schema、依赖、Hash/签名、Migration、隔离模式、Tool 名称冲突）
→ installed
→ configured
→ enabled
→ started
→ running
→ degraded/disabled
→ stopped
→ uninstalled
```

### 7.4 生命周期接口

```python
class PluginLifecycle(Protocol):
    async def install(self, context): ...
    async def start(self, context): ...
    async def health(self) -> HealthStatus: ...
    async def stop(self, context): ...
    async def uninstall(self, context): ...
```

### 7.5 隔离模式

```text
in_process_trusted   内置 + 用户明确信任
subprocess           默认第三方插件运行方式
sandbox              高风险插件：限制文件、网络、进程
remote_mcp           外部 MCP Server 作为远程进程运行
```

第三方插件默认不使用 `in_process_trusted`。

### 7.6 插件故障处理

```text
单次调用失败        返回标准 Error
连续失败            熔断（circuit breaker: 3 次 / 60s 窗口）
启动失败            插件 disabled/degraded，不影响 Core 启动
进程崩溃            重启或禁用
Schema 不兼容       拒绝加载
超预算              终止调用
权限违规            拒绝并 Audit
```

插件失败不得使 Agent Core 无法启动，除非标记为 `required: true`。

运行时公开面固定为：`PluginRuntime` Port、`SqlitePluginRuntime` 唯一状态写入
实现、`PluginProcessSupervisor` 第三方进程生命周期、`PluginPolicyAdapter`
权限映射。`enabled` 只表示允许启动；完成宿主进程 ready 握手后才能进入
`running`。Core 重启时旧 `running` 记录按 `stopped` 恢复，不假设旧 PID
仍有效。

第三方宿主使用参数数组启动、最小环境变量、独立工作目录和显式停止协议。
Manifest 权限必须是配置 Grant 的子集；越权或启动失败进入 degraded 并写
Plugin Runtime Audit。升级覆盖现有版本前保存 Manifest/状态快照，rollback
恢复最近快照后保持 installed，需重新 enable/start。

---

## 8. 版本与依赖冲突

- Core API 使用语义版本；
- Plugin 声明 `api_version` 支持范围；
- Capability 使用全局唯一 ID（`tool_id` 按 `namespace:name` 命名）；
- 同一 Slot 同时只有一个 Active Provider；
- Provider 失败切换由 Core Router 决定；
- 插件升级前保存配置和状态快照，支持回滚。

---

## 9. 指标与测试

指标：Tool 执行数、拒绝率、审批率、熔断触发数、Skill 加载数（按 active/stale/archived）、插件故障率、MCP Server 可用性。

测试覆盖：

- 内置 Tool 自动发现（所有 `tools/*.py` 可正确 import 和注册）；
- Toolset 模式分发（不同模式加载不同的工具列表）；
- 插件多源发现（同名覆盖优先级）；
- SKILL.md frontmatter 解析（所有字段、边界值、错误格式）；
- Skill 生命周期状态转移；
- 插件隔离模式（subprocess 崩溃不拖垮 Core）；
- MCP Server Schema 校验和命名冲突检测。
