# Reference Projects 分析与 cogito-v1 缺失能力报告

> 分析范围：`Cogito-Agent/references/` 下 5 个项目（akashic-agent, gemini-cli, hermes-agent, nanobot, QwenPaw）  
> 对比基准：当前 `cogito-v1` 项目  
> 生成日期：2026-06-25

---

## 目录

1. [项目总览](#1-项目总览)
2. [各项目 MCP 实现对比](#2-各项目-mcp-实现对比)
3. [各项目 Tool 实现对比](#3-各项目-tool-实现对比)
4. [各项目 MCP/Tool 安全防护对比](#4-各项目-mcptool-安全防护对比)
5. [cogito-v1 已有的防护体系](#5-cogito-v1-已有的防护体系)
6. [cogito-v1 缺失的关键能力（优先级排序）](#6-cogito-v1-缺失的关键能力优先级排序)
7. [建设路线图建议](#7-建设路线图建议)

---

## 1. 项目总览

| 项目 | 语言 | 定位 | MCP 传输 | 工具体系 | 防护强度 |
|------|------|------|----------|----------|----------|
| **akashic-agent** | Python | 通用 Agent 框架 | stdio | Tool ABC + Registry + Hooks + Executor | 中上 |
| **gemini-cli** | TypeScript | Google 官方 CLI Agent | stdio/SSE/HTTP (docs) | Policy Engine + 多层防护 | 高（企业级） |
| **hermes-agent** | Python | 安全优先 Agent CLI | stdio + 安全预检 | ToolExecutor + Guardrails | 高 |
| **nanobot** | Python | 轻量 Agent 框架 | stdio/SSE/StreamableHTTP | Tool ABC + Registry + Sandbox | 中上 |
| **QwenPaw** | Python | 生产级 Agent 平台 | stdio/HTTP + OAuth | ToolGuardEngine + 三层 Guardian | 最高 |
| **cogito-v1** | Python | DDD 架构 Agent | stdio/SSE/StreamableHTTP | Registry + Orchestrator (19步) | 中（结构好但细节缺） |

---

## 2. 各项目 MCP 实现对比

### 2.1 连接管理

| 功能 | akashic-agent | nanobot | QwenPaw | cogito-v1 |
|------|:---:|:---:|:---:|:---:|
| 基本 Client 实现 | ✅ McpClient (stdio) | ✅ connect_mcp_servers | ✅ StatefulClient | ✅ MCPClient |
| stdio 传输 | ✅ | ✅ | ✅ | ✅ StdioMCPTransport |
| SSE 传输 | ❌ | ✅ | ✅ | ✅ SSEMCPTransport |
| Streamable HTTP 传输 | ❌ | ✅ | ✅ | ✅ StreamableHTTPTransport |
| 多 Server 管理 | ✅ McpServerRegistry | ✅ 按 Server 独立栈 | ✅ MCPClientManager | ✅ MCPClientManager |
| 服务状态枚举 | ❌ | ❌（仅 connected set） | ❌ | ✅ ServerState(8种) |
| 并发连接限制 | ❌ | ❌ | ❌ | ✅ Semaphore(4) |
| 连接超时 | ✅ 8s | ✅ 3s TCP probe | ✅ 可配 | ❌（硬编码 30s） |
| 连接重试 | ❌ | ❌ | ✅ 后台重连 | ❌（单次失败即放弃） |
| 热重载 | ❌ | ✅ reload_servers | ✅ replace_client | ❌ |
| 断开清理 | ✅ terminate/kill | ✅ stack.aclose | ✅ close_all + orphan kill | ✅ kill + wait（简陋） |

### 2.2 工具发现与包装

| 功能 | akashic-agent | nanobot | QwenPaw | cogito-v1 |
|------|:---:|:---:|:---:|:---:|
| 工具列表发现 | ✅ tools/list | ✅ session.list_tools | ✅ client tool list | ✅ |
| 资源(Resource)包装 | ❌ | ✅ MCPResourceWrapper | ❌ | ❌ |
| 提示(Prompt)包装 | ❌ | ✅ MCPPromptWrapper | ❌ | ❌ |
| 工具名 sanitize | ✅ mcp_server__tool | ✅ _sanitize_name | ✅ store | ✅ mcp_server_tool |
| Schema 规范化 | ❌（透传） | ✅ nullable/oneOf 处理 | ❌ | ✅ $ref 移除 + additionalProperties |
| 工具过滤（include/exclude） | ❌ | ✅ enabled_tools | ✅ tool_whitelist | ✅ include/exclude |
| 工具变更通知 | ❌ | ✅ notify_tools_changed | ❌ | ✅ 框架支持 |
| 超时控制 | ✅ 可配 | ✅ tool_timeout | ✅ timeout 参数 | ✅ ToolDefinition 级别 |
| 临时性错误重试 | ❌ | ✅ 单次重试 | ✅ 生命周期重连 | ❌ |

### 2.3 MCP 安全入口校验

| 功能 | akashic-agent | nanobot | QwenPaw | hermes-agent | cogito-v1 |
|------|:---:|:---:|:---:|:---:|:---:|
| HTTP URL SSRF 校验 | ✅ | ✅ validate_url_target | ✅ OAuth | ✅ mcp_security | ❌（transport 层未做） |
| TCP 预连接探测 | ❌ | ✅ _probe_http_url | ❌ | ❌ | ❌ |
| HTTP 请求事件钩子 | ❌ | ✅ event_hooks.request | ❌ | ❌ | ❌ |
| Windows 命令包装 | ❌ | ✅ cmd /d /c 包装 | ✅ 类似 | ❌ | ❌ |
| Shell 解释器检测 | ❌ | ❌ | ❌ | ✅ validate_mcp_server_entry | ❌ |
| 命令 egress 检测 | ❌ | ❌ | ❌ | ✅ curl/wget in args | ❌ |

---

## 3. 各项目 Tool 实现对比

### 3.1 工具注册与目录

| 功能 | akashic-agent | nanobot | QwenPaw | cogito-v1 |
|------|:---:|:---:|:---:|:---:|
| 基类/协议 | ✅ Tool (ABC) | ✅ Tool (ABC) | ✅ Tool (ABC) | ✅ ToolHandler (Protocol) |
| 注册中心 | ✅ ToolRegistry | ✅ ToolRegistry | ✅ 内部注册 | ✅ ToolRegistryPort (versioned) |
| 风险等级元数据 | ✅ read-only/write/ext | ❌ | ✅ ToolRisk | ✅ ToolRisk (6级) |
| 搜索引擎 | ✅ KeywordSearchBackend | ❌ | ❌ | ❌ |
| 按需解锁(deferred tools) | ✅ tool_search | ❌ | ❌ | ❌ |
| 快照机制 | ❌ | ❌ | ❌ | ✅ versioned snapshot |
| Provider 级别原子替换 | ❌ | ❌ | ❌ | ✅ replace_provider_tools |

### 3.2 工具执行管道

| 功能 | akashic-agent | nanobot | QwenPaw | cogito-v1 |
|------|:---:|:---:|:---:|:---:|
| 执行管道步数 | 3步（pre→exec→post） | 简版 | 3步（guard→exec→result） | 19步（完整） |
| Schema 校验 | ✅ validate_params | ❌（SDK） | ✅ 内置 | ✅ JsonSchemaToolValidator |
| 并发控制 | ❌ | ❌ | ❌ | ✅ ToolConcurrencyController |
| 结果处理 | ❌ | ✅ result_processor | ❌ | ✅ DefaultToolResultProcessor |
| 超时控制 | ✅ timeout 参数 | ✅ timeout | ✅ timeout | ✅ asyncio.timeout |
| 参数类型强制 | ❌ | ❌ | ❌ | ✅ type coercion |
| 输出校验 | ❌ | ❌ | ❌ | ✅ output_schema validation |
| 上下文中继 | ✅ ToolExecutionRequest | ✅ tool_context | ❌ | ✅ ToolExecutionContext |
| 重复调用防护 | ❌ | ❌ | ❌ | ✅ RepetitionGuard |

### 3.3 Shell 工具

| 功能 | akashic-agent | nanobot | QwenPaw | cogito-v1 |
|------|:---:|:---:|:---:|:---:|
| 命令黑名单 | ✅ nc/telnet/浏览器 | ❌ | ❌ | ❌（改用白名单） |
| 命令白名单 | ❌ | ❌ | ❌ | ✅ allowed_commands |
| 危险模式匹配 | ❌ | ❌ | ❌ | ✅ 18种危险正则 |
| 网络命令护栏 | ✅ curl/wget URL+Flag 校验 | ✅ SSRF 校验 | ✅ path-based | ✅ NetworkPolicy |
| 目录限制 | ✅ restricted_dir | ✅ workspace | ✅ workspace | ✅ workspace_scope |
| 后台任务管理 | ✅ task_output/task_stop | ❌ | ❌ | ❌ |
| 输出截断 | ✅ 30K字符 | ✅ 类似 | ✅ 类似 | ❌（无显式截断） |
| 自动转后台 | ✅ 15s阈值 | ❌ | ❌ | ❌ |
| 环境变量白名单 | ❌ | ❌ | ❌ | ✅ allowed_env_keys |
| Shell 子进程创建标志 | ✅ NEW_PROCESS_GROUP | ❌ | ❌ | ✅ (简陋) |

### 3.4 文件系统工具

| 功能 | akashic-agent | nanobot | QwenPaw | cogito-v1 |
|------|:---:|:---:|:---:|:---:|
| 读取 | ✅ allowed_dir | ✅ path_utils | ✅ workspace_check | ✅ WorkspaceScope |
| 写入 | ✅ allowed_dir | ✅ workspace | ✅ workspace | ✅ WorkspaceScope |
| 编辑 | ✅ old→new 替换 | ✅ old→new + patch | ✅ edit | ❌（尚未实现） |
| 列表目录 | ✅ allowed_dir | ✅ | ✅ | ❌（尚未实现） |
| 读取截断 | ✅ 400行/10KB | ✅ 类似 | ✅ 类似 | ❌ |
| 并发写锁 | ✅ asyncio.Lock per file | ❌ | ❌ | ❌ |
| 图片读取 | ✅ base64 + ToolResult | ✅ | ✅ | ❌ |
| 二进制检测 | ✅ _looks_binary | ❌ | ❌ | ❌ |

### 3.5 Web 工具

| 功能 | akashic-agent | nanobot | QwenPaw | cogito-v1 |
|------|:---:|:---:|:---:|:---:|
| HTTP 抓取 | ✅ web_fetch | ✅ web_fetch | ✅ | ❌（尚未实现） |
| 搜索引擎 | ✅ web_search (Exa) | ✅ web_search | ✅ | ❌ |
| SSRF 防护 | ✅ validate_url_target | ✅ validate_url_target | ✅ | ✅ DefaultNetworkPolicy |
| 大小限制 | ✅ 5MB | ✅ | ✅ | ✅ (10MB config) |
| HTML→Markdown 转换 | ✅ html2text | ✅ | ✅ | ❌ |
| 压缩炸弹防护 | ❌ | ❌ | ❌ | ❌ |
| URL 凭证检查 | ❌ | ❌ | ✅ | ✅ |
| 重定向再校验 | ❌ | ✅ validate_resolved_url | ❌ | ✅ check_redirect |

---

## 4. 各项目 MCP/Tool 安全防护对比

### 4.1 工具调用防护体系总览

| 防护层 | akashic-agent | nanobot | QwenPaw | hermes-agent | cogito-v1 |
|--------|:---:|:---:|:---:|:---:|:---:|
| **① 命令策略** | 黑名单+网络校验 | workspace 检查 | YAML 规则 | tool_guardrails | 白名单+危险模式 |
| **② 路径安全** | allowed_dir | workspace_scope | workspace + 敏感文件 | ❌ | workspace_scope |
| **③ 网络 SSRF** | IP+域名校验 | DNS+CIDR 校验 | URL 校验 | ❌ | IP+域名校验 |
| **④ Shell 规避检测** | ❌ | ❌ | ✅ 7种检测器 | ❌ | ❌ |
| **⑤ Hook 管道** | ✅ pre/post hooks | ❌ | ✅ Guardian | ✅ Guardrails | ✅ Policy 层 |
| **⑥ 用户审批** | ❌ | ❌ | ✅ approval UI | ❌ | ✅ approval port |
| **⑦ 速率限制** | ❌ | ❌ | ❌ | ❌ | ✅ rate_limit port |
| **⑧ 秘密脱敏** | ❌ | ❌ | ❌ | ❌ | ✅ SecretRedactor |
| **⑨ Sandbox** | ❌ | ✅ bwrap | ❌ | ❌ | ✅ SandboxPort |
| **⑩ MCP 入口** | ❌ | ✅ URL 校验 | ✅ OAuth | ✅ shell+egress | ❌ |

### 4.2 QwenPaw 三层 Guardian（cogito-v1 最缺失的能力）

QwenPaw 的 `ToolGuardEngine` 是目前看到的最完善的工具防护体系：

```
ToolGuardEngine
├── FilePathToolGuardian     # 路径敏感文件防护
│   ├── 已知文件工具的路径参数检查 (read_file/write_file/edit_file/...)
│   ├── Shell 命令中提取路径扫描 (shlex.split + 重定向检测)
│   └── 所有工具字符串参数的路径启发式检查
│
├── RuleBasedToolGuardian    # YAML 规则匹配（核心）
│   ├── 规则格式: id/tool/params/category/severity/patterns/exclude_patterns
│   ├── 内置规则: dangerous_shell_commands.yaml
│   ├── 自定义规则: config.security.tool_guard.custom_rules
│   ├── rm 命令特殊增强: 检测 workspace 外删除
│   └── 禁用规则: config.security.tool_guard.disabled_rules
│
└── ShellEvasionGuardian     # Shell 规避/混淆检测
    ├── 命令替换检测: $()  /  `  /  <>  /  =()  /  ${}
    ├── 标志混淆检测: $'' / $"" / 空引号+短横
    ├── 反斜杠转义空白检测
    ├── 反斜杠转义操作符检测 (\\; \\| \\& \\< \\>)
    ├── 换行隐藏命令检测
    ├── 注释引号失谐检测 (#内部引号)
    └── 引号内换行+#行攻击检测
```

**执行级别控制：**
- `STRICT`: 所有工具需审批
- `SMART`: INFO/LOW 自动放行，MEDIUM+ 需审批（推荐）
- `AUTO`: 仅 guarded_tools 列表检查（兼容旧行为）
- `OFF`: 完全禁用

### 4.3 Akashic-Agent ToolHook 管道

```
ToolExecutor
├── 1. pre_hooks  (pre_tool_use)
│   ├── match() → 是否匹配
│   ├── run() → 修改参数 / 拒绝执行
│   └── decision: pass | deny
│
├── 2. invoker (真实执行)
│   └── ToolRegistry.execute(name, arguments)
│
└── 3. post_hooks (post_tool_use / post_tool_error)
    └── 记录 / 补充信息
```

**关键特性：**
- Hook 是唯一可修改输入/直接拒绝的阶段
- 每调用含 pre_trace / post_trace 记录
- HookExecutionError 兜底
- ToolExecutionRequest 携带 source (passive/proactive/subagent)
- 可配置风险等级 (read-only/write/external-side-effect)

### 4.4 Nanobot 安全体系

- **WorkspaceScope**: 按 channel 区分 restricted/full 模式，WebUI 可调
- **SSRF**: DNS 解析 + IP CIDR 检查 + 可配白名单 (tailscale 等)
- **Sandbox**: bwrap 后端，可扩展 _wrap_<name>
- **MCP HTTP 校验**: 事件钩子 + 重定向校验
- **路径工具**: path_utils 统一 resolve

### 4.5 Hermes MCP 安全

专注于 MCP Server 配置时的预检查：
- 检测 Shell 解释器 (bash/sh/zsh/cmd/powershell)
- 检测 egress 命令 (curl/wget/nc/ncat/socat)
- 检测数据泄漏形态 (--data-binary, POST, .env)
- 安全建议/审计报告

---

## 5. cogito-v1 已有的防护体系

cogito-v1 的架构设计已经考虑到了许多安全维度，部分实现已到位：

| 模块 | 状态 | 强度 |
|------|:----:|:----:|
| **CommandPolicy** | ✅ 已实现 | 中 - 白名单+危险模式，缺 shell 规避检测 |
| **DefaultNetworkPolicy** | ✅ 已实现 | 中 - IP+域名校验，缺 DNS 解析重定向检查 |
| **DefaultWorkspaceScope** | ✅ 已实现 | 高 - 含 Windows 设备路径/UNC/ADS 拦截 |
| **DefaultSecretRedactor** | ✅ 已实现 | 高 - 模式+键名+后缀三段脱敏 |
| **CompositeToolPolicyEngine** | ✅ 已实现 | 中 - 6层策略组合，缺危险度动态判断 |
| **ToolApprovalCoordinatorPort** | ✅ Port 定义 | 低 - 待完整实现 durable approval |
| **ToolRateLimiterPort** | ✅ Port 定义 | 低 - 待完整实现 |
| **ToolSandboxPort** | ✅ Port 定义 | 低 - 待完整实现 |
| **RepetitionGuard** | ✅ 已实现 | 中 |
| **ConcurrencyController** | ✅ 已实现 | 中 |
| **Schema 校验** | ✅ 已实现 | 高 |
| **结果脱敏** | ✅ 集成在 ResultProcessor | 高 |
| **MCP SSRF (transport)** | ❌ 未实现 | - |
| **MCP 临时错误重试** | ❌ 未实现 | - |
| **Shell 规避检测** | ❌ 未实现 | - |
| **YAML 规则系统** | ❌ 未实现 | - |

---

## 6. cogito-v1 缺失的关键能力（优先级排序）

### 🔴 P0 — 安全关键缺口

| # | 缺失能力 | 参考来源 | 重要性 |
|---|---------|---------|:------:|
| 1 | **Shell Evasion Guardian** — 检测 shell 混淆/规避攻击（7种检测器） | QwenPaw | 攻击者可绕过当前 CommandPolicy 的白名单检查 |
| 2 | **MCP 连接入口安全** — MCP Server URL SSRF 校验、TCP 预探测、HTTP 请求事件钩子 | nanobot/hermes-agent | MCP Server 可能连接恶意内网服务 |
| 3 | **YAML 规则引擎** — 可配置的规则系统 (tool/param/pattern/severity) | QwenPaw | 当前规则硬编码在 Python 中，无法热更新 |
| 4 | **敏感文件路径防护 (FilePathToolGuardian)** — 跨所有工具统一拦截 | QwenPaw | 当前仅 Shell 工具的 directory restriction |

### 🟡 P1 — 功能健全缺口

| # | 缺失能力 | 参考来源 | 重要性 |
|---|---------|---------|:------:|
| 5 | **MCP 后台任务管理** — shell 的 run_in_background / task_output / task_stop | akashic-agent | 无法管理长时间运行的后台命令 |
| 6 | **Shell 输出截断** — 超过 30K 字符智能截断（保留尾部） | akashic-agent | 大数据输出撑爆上下文 |
| 7 | **ToolHook 管道** — 可插拔 pre/post 钩子 | akashic-agent | 当前 hook 点不足 |
| 8 | **Tool 搜索/发现** — 关键词搜索 + 按需解锁 | akashic-agent | 模型不知道有哪些工具可用 |
| 9 | **MCP 热重载** — 不重启服务更新 MCP 配置 | nanobot/QwenPaw | 当前修改 MCP 需重启 |
| 10 | **MCP Resource/Prompt 包装** — 利用 MCP 服务全部能力 | nanobot | 当前仅包装工具 |
| 11 | **文件编辑工具** — old→new 替换和补丁 | nanobot/akashic-agent | 尚缺失 |

### 🔵 P2 — 体验与运维缺口

| # | 缺失能力 | 参考来源 |
|---|---------|---------|
| 12 | **MCP 连接超时配置化** | QwenPaw (可配 default) |
| 13 | **MCP 窗口命令适配 (cmd /d /c 包装)** | nanobot |
| 14 | **subagent/spawn 工具** — 委派子任务 | akashic-agent |
| 15 | **工具风险等级元数据 + 搜索索引** | akashic-agent |
| 16 | **Sandbox 后端 (bwrap)** | nanobot |
| 17 | **审计跟踪增强** — pre/post hook trace | akashic-agent |
| 18 | **工具审批 UI 交互嵌入工具结果** | QwenPaw |

### 🟢 P3 — 锦上添花

| # | 缺失能力 | 参考来源 |
|---|---------|---------|
| 19 | MCP OAuth 支持 | QwenPaw |
| 20 | A2A (Agent-to-Agent) 工具 | QwenPaw |
| 21 | MCP 孤儿进程清理 | QwenPaw |
| 22 | LSP 代码智能工具 | QwenPaw |
| 23 | ACP 适配层 | hermes-agent |
| 24 | 社区技能扫描器 | QwenPaw |

---

## 7. 建设路线图建议

### Phase 1 — 安全加固（立即实施）

```
1. Shell Evasion Guardian ──────── 2-3 天
   ├── 命令替换检测 ($() / `` / <() / =())
   ├── 标志混淆检测 ($'' / $"" / 空引号+短横)
   ├── 反斜杠转义检测 (空白/操作符)
   ├── 换行隐藏命令检测
   └── 注释引号失谐 + 引号内换行攻击检测

2. MCP 连接入口安全 ───────────── 1-2 天
   ├── transport 层 URL SSRF 校验
   ├── TCP 预连接探测 (参考 nanobot _probe_http_url)
   ├── HTTP 请求事件钩子 (重定向再校验)
   └── Windows 命令包装

3. YAML 规则引擎 ──────────────── 3-4 天
   ├── GuardRule 数据模型 (id/tool/param/pattern/severity)
   ├── YAML 加载器 + 编译正则
   ├── 内置 dangerous_shell_commands.yaml
   ├── 自定义规则支持 (配置)
   └── 禁用规则支持
```

### Phase 2 — 功能健全（短期）

```
4. ToolHook 管道 ──────────────── 2-3 天
   ├── ToolHook ABC (name/matches/run)
   ├── ToolExecutor (pre → invoker → post)
   └── Pre-hook 拒绝路径 (deny reason → synthetic error)

5. Shell 后台任务 ────────────── 1-2 天
   ├── run_in_background 参数
   ├── task_output 轮询接口
   └── task_stop 终止接口

6. 文件编辑工具 ───────────────── 1-2 天
   ├── edit_file (old→new 替换)
   └── apply_patch (补丁应用)

7. MCP 热重载 ────────────────── 2-3 天
   ├── config 文件监听
   ├── 差异化连接/断开
   └── RuntimeControl 事件
```

### Phase 3 — 体验优化（中长期）

```
8. Tool 搜索引擎 ──────────────── 2-3 天
   ├── keyword search backend
   ├── tool_search 工具
   └── deferred tool loading

9. MCP Resource/Prompt 包装 ───── 1-2 天

10. 执行级别控制 ───────────────── 1 天
    ├── STRICT / SMART / AUTO / OFF
    └── 配置化阈值

11. Spawn 子任务 ──────────────── 3-5 天
    ├── subagent manager
    ├── 权限 profile (research/scripting/general)
    └── 策略决策
```

---

## 快速对比表：各项目工具完整度

| 类别 | akashic-agent | nanobot | QwenPaw | cogito-v1 |
|------|:---:|:---:|:---:|:---:|
| MCP 工具 | ✅ | ✅✅✅ | ✅✅ | ✅✅ |
| Shell 工具 | ✅✅✅ | ✅ | ✅ | ✅ |
| 文件读取 | ✅✅ | ✅✅ | ✅✅ | ✅（缺 truncation） |
| 文件写入 | ✅ | ✅ | ✅ | ✅ |
| 文件编辑 | ✅ | ✅ | ✅ | ❌ |
| 目录列表 | ✅ | ✅ | ✅ | ❌ |
| Web 抓取 | ✅✅ | ✅✅ | ✅ | ❌ |
| Web 搜索 | ✅ | ✅ | ✅ | ❌ |
| 搜索/发现 | ✅ | ❌ | ❌ | ❌ |
| Spawn 子任务 | ✅ | ✅ | ✅ | ❌ |
| 审批流程 | ❌ | ❌ | ✅✅ | ✅（port only） |
| 速率限制 | ❌ | ❌ | ❌ | ✅（port only） |
| 秘密脱敏 | ❌ | ❌ | ❌ | ✅ |
| Shell 规避检测 | ❌ | ❌ | ✅✅✅ | ❌ |
| YAML 规则引擎 | ❌ | ❌ | ✅✅ | ❌ |
| 敏感文件拦截 | ❌ | ✅ | ✅✅ | ❌ |
| MCP 热重载 | ❌ | ✅✅ | ✅ | ❌ |
| 审计跟踪 | ✅ | ❌ | ❌ | ❌ |
| 后台任务 | ✅✅✅ | ❌ | ❌ | ❌ |

> **注**: ✅✅✅ = 最完善实现 / ✅✅ = 有但可改进 / ✅ = 基本实现 / ❌ = 缺失

---

## 核心结论

1. **架构层面 cogito-v1 领先**：19步执行管道、versioned registry、DDD 分层是其他项目没有的
2. **安全层面 QwenPaw 最完善**：三层 Guardian + 执行级别 + YAML 规则引擎，是填补缺口的主要参考
3. **MCP 层面 nanobot 最成熟**：三种传输 + 热重载 + Resource/Prompt 包装
4. **工具层面 akashic-agent 最全面**：shell 后台任务、spawn、tool search、hook 管道
5. **当前最大风险缺口是 P0 的三项**：Shell Evasion、MCP 入口安全、YAML 规则引擎
