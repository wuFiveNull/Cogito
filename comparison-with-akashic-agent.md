# Cogito v1 vs Akashic Agent — 详细对比分析

> 分析日期：2026-06-25
> 对比范围：两个项目的架构、设计理念、实现细节

---

## 目录

1. [项目总览与目标定位](#1-项目总览与目标定位)
2. [架构风格与设计哲学](#2-架构风格与设计哲学)
3. [回合处理管道](#3-回合处理管道)
4. [内存/记忆系统](#4-内存记忆系统)
5. [LLM 集成](#5-llm-集成)
6. [工具系统](#6-工具系统)
7. [插件/扩展系统](#7-插件扩展系统)
8. [通信频道](#8-通信频道)
9. [主动推送与后台任务](#9-主动推送与后台任务)
10. [配置系统](#10-配置系统)
11. [数据持久化](#11-数据持久化)
12. [安全与沙箱](#12-安全与沙箱)
13. [测试策略](#13-测试策略)
14. [技术栈与依赖](#14-技术栈与依赖)
15. [代码规模与项目健康度](#15-代码规模与项目健康度)
16. [关键设计决策对比](#16-关键设计决策对比)
17. [总结与建议](#17-总结与建议)

---

## 1. 项目总览与目标定位

| 维度 | Cogito v1 | Akashic Agent |
|------|-----------|---------------|
| **项目名称** | Cogito — Agentic AI assistant framework | akashic-agent — 一个会主动找你的 AI 伙伴 |
| **核心定位** | **通用 AI 助手框架**，频道无关、领域驱动的 agent 引擎 | **实际运行的 AI 伙伴**，强调主动推送和长期陪伴 |
| **目标用户** | 开发者（框架使用者） | 终端用户（可直接运行的 bot） |
| **主要语言** | Python 3.12+ | Python 3.12+ |
| **代码行数** | ~358 个 .py 文件，估计 25k-35k 行 | ~280+ 个 .py 文件，估计 40k-50k 行（含大量测试） |
| **启动方式** | `python -m cogito` | `python main.py [setup\|cli\|dashboard\|gateway]` |
| **成熟度阶段** | 基础框架完成（8 阶段管道稳定），正在完善工具和持久化 | 功能更丰富，有实际部署经验，社区（QQ 群） |
| **文档语言** | 中文（设计规范）、英文（代码注释） | 中文（README/Handbook）、英文（代码） |
| **前端** | 纯 Python asyncio HTTP 内嵌聊天 UI | React/TypeScript Dashboard + Textual TUI CLI |

**一句话总结**：Cogito 是一个**框架**——它提供了构建 AI agent 所需的所有抽象和管道，但需要开发者集成才能运行。Akashic Agent 是一个**产品**——它开箱即用，配置好就能跑起来，甚至已经考虑了让 agent 主动找用户聊天。

---

## 2. 架构风格与设计哲学

### Cogito v1 —— 严格的领域驱动设计（DDD）

```
┌─────────────────────────────────────────────┐
│                  DDD Layers                   │
│  ┌───────────┐  ┌──────────┐  ┌──────────┐  │
│  │  Domain   │  │  Ports   │  │  Infra   │  │
│  │  (模型)    │  │  (接口)   │  │  (实现)   │  │
│  └─────┬─────┘  └────┬─────┘  └────┬─────┘  │
│        └──────┬──────┘              │        │
│               │  Application Layer  │        │
│               └──────────────────────┘       │
│  ┌──────────────────────────────────────┐     │
│  │        Bootstrap (DI 容器)            │     │
│  └──────────────────────────────────────┘     │
└─────────────────────────────────────────────┘
```

**关键原则**：
- **严格的层间依赖规则**：Domain → Ports ← Infra（依赖倒置）
- **端口/适配器分离**（Hexagonal 风格）：`cogito/agent/ports/` 定义抽象接口，`cogito/infrastructure/` 提供具体实现
- **固定顺序管道**：8 个阶段由 `RuntimeKernel` 按硬编码顺序执行，禁止拓扑排序
- **依赖注入**：所有组件通过工厂函数在 `bootstrap/` 中组装
- **事务性持久化**：`UnitOfWork` 模式保证 13 步写入的原子性

### Akashic Agent —— 灵活的插件式生命周期

```
┌──────────────────────────────────────────────┐
│              agent/lifecycle/                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐ │
│  │ Phase 链  │  │  EventBus │  │ PluginManager │ │
│  │ (7 phases)│  │ (9 events)│  │ (热插拔)     │ │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘ │
│       └──────┬──────┘               │          │
│              │  AgentLoop (主循环)    │          │
│              └───────────────────────┘          │
│  ┌──────────────────────────────────────┐      │
│  │  4 种插件介入方式                      │      │
│  │  PhaseModule / EventBus / @on_tool   │      │
│  │  @tool 装饰器                         │      │
│  └──────────────────────────────────────┘      │
└──────────────────────────────────────────────┘
```

**关键原则**：
- **插件优先**：几乎所有行为扩展都通过插件实现（13 个内置插件）
- **生命周期事件驱动**：7 个 Phase 阶段 + 9 种 EventBus 事件 + 4 种插件挂载点
- **渐进式组合**：`Slot` 机制允许插件声明依赖，框架进行拓扑排序
- **实用主义**：不追求严格的 DDD 分层，更注重快速迭代和功能丰富
- **三种运行模式**：被动回复、主动推送、Drift 后台任务

### 架构哲学对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **DDD 遵循度** | ⭐⭐⭐⭐⭐ 严格遵循 | ⭐⭐ 部分借鉴 |
| **依赖注入** | ⭐⭐⭐⭐⭐ 构造函数注入 + 工厂函数 | ⭐⭐⭐ 手工 wire |
| **扩展性机制** | 固定阶段 + 策略/端口替换 | 插件系统 + 生命周期钩子 |
| **测试友好度** | ⭐⭐⭐⭐⭐ 端口隔离，易于 mock | ⭐⭐⭐⭐ 插件可 mock，但耦合较紧 |
| **学习曲线** | 较陡（DDD 概念多） | 较平缓（约定优于配置） |
| **模块化严格度** | 高（层间依赖受控） | 中（允许跨层调用） |
| **运行时灵活性** | 低（阶段顺序固定） | 高（插件可改变行为） |

---

## 3. 回合处理管道

### Cogito v1 —— 8 阶段固定管道

```
AgentRequest
    │
    ▼
┌──────────────────────────────────────────────────────┐
│  RuntimeKernel (cogito/agent/runtime/kernel.py)       │
│                                                       │
│  Phase 1: TurnInitPhase       验证请求，初始化上下文     │
│  Phase 2: StateLoadPhase      从 SQLite 加载状态        │
│  Phase 3: InformationRetrievalPhase  并发检索多个源     │
│  Phase 4: ContextAssemblyPhase    构建模型消息列表      │
│  Phase 5: AgentLoopPhase      推理-行动循环            │
│  Phase 6: KnowledgeExtractionPhase   提取知识候选项    │
│  Phase 7: PersistencePhase    原子事务写入 SQLite      │
│  Phase 8: TurnFinalizePhase   封装不可变 TurnResult    │
│                                                       │
└──────────────────────────────────────────────────────┘
    │
    ▼
TurnResult
```

**特点**：
- 顺序**硬编码**在 `RuntimeKernel.run()` 中
- 每个阶段通过 `TurnContext` 共享可变状态
- 使用 `AgentEvent` 事件发布机制跟踪阶段生命周期
- 错误映射器将异常统一转为 `RuntimeAgentError`
- 支持暂停（`WAITING_APPROVAL`）和取消（`CANCELLED`）状态

### Akashic Agent —— 6 阶段生命周期 + 3 种运行模式

```
InboundMessage
    │
    ▼
┌──────────────────────────────────────────────────────┐
│  AgentCore — passive_turn.py                          │
│                                                       │
│  Phase 1: BeforeTurn      获取 session，准备上下文      │
│  Phase 2: BeforeReasoning  前置决策门                   │
│  Phase 3: PromptRender    组装系统提示词                │
│  Phase 4: Reasoner (loop)  LLM 调用 + 工具循环          │
│  Phase 5: AfterReasoning  后处理推理结果                │
│  Phase 6: AfterTurn       触发记忆 consolidation       │
│                                                       │
└──────────────────────────────────────────────────────┘
    │
    ▼
三种运行模式：
├── PassiveTurn (被动回复) — 以上 6 阶段
├── ProactiveTurn (主动推送) — 轻量版管道 + LLM 决策
└── DriftTurn (空闲任务) — SKILL.md 驱动的后台执行
```

**特点**：
- 每个 Phase 内部有 **Module 链**（插件提供）
- Module 通过 `Slot` 声明依赖，运行时拓扑排序
- 3 种运行模式共享部分阶段实现
- 使用 `EventBus` 发布生命周期事件（9 种事件类型）
- 每个 Phase 有可选的 `GATE` 机制（插件可中止流程）

### 管道对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **阶段数量** | 8 个固定阶段 | 6 个阶段 × 3 种运行模式 |
| **阶段排序** | 硬编码顺序 | Phase 内部 Module 链拓扑排序 |
| **可变性** | 低（改顺序需改 kernel.py） | 高（插件可增减 Module） |
| **状态共享** | TurnContext（可变数据类） | 多个 `*Ctx` dataclass |
| **事件系统** | AgentEvent（阶段级事件） | EventBus（9 种细粒度事件） |
| **错误处理** | ErrorMapper 统一映射 | try/except + 重试策略 |
| **流式支持** | Phase 5 内处理 | Phase 4 Reasoner 内处理 |
| **中断机制** | CancellationToken | InterruptController |
| **事务边界** | Phase 7 PersistencePhase 原子提交 | AfterTurn 触发 consolidation |

---

## 4. 内存/记忆系统

### Cogito v1 —— 检索驱动的记忆系统

```
                    ┌──────────────────┐
                    │  RetrievalPhase   │
                    │  (Phase 3)        │
                    └────────┬─────────┘
                             │ 并发检索
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   ┌────────────┐     ┌────────────┐     ┌────────────┐
   │ 关键词检索  │     │  向量检索   │     │  偏好检索   │
   │ (FTS5)     │     │ (embedding) │     │ (规则匹配)  │
   └────────────┘     └────────────┘     └────────────┘
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   ┌────────────┐     ┌────────────┐     ┌────────────┐
   │ 历史消息    │     │ 长期记忆    │     │  用户画像   │
   └────────────┘     └────────────┘     └────────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  RRF Fusion       │
                    │  重排序 + 多样性选择│
                    └──────────────────┘
```

**实现方式**：
- 5 个 `RetrieverAdapter`（关键词/向量/偏好/历史/长期记忆）
- 检索发生在**回合开始时**（Phase 3）
- 使用加权 RRF（Reciprocal Rank Fusion）融合多源结果
- 记忆存储在 SQLite 的 `memories` 表中
- 知识提取在**回合结束时**（Phase 6）异步进行

**持久化位置**：
- `memories` 表（结构化记忆 + embedding）
- `events` 表（时间线事件）
- `trace_events` 表（追踪数据）

### Akashic Agent —— 双层的持续记忆系统

```
                    ┌───────────────────┐
                    │  每回合自动注入     │
                    │  Markdown 记忆块   │
                    └────────┬──────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   ┌────────────┐     ┌────────────┐     ┌────────────┐
   │ Markdown   │     │ memory2.db │     │ Akasha     │
   │ 文件系统    │     │ 向量 SQLite │     │ 图记忆引擎  │
   └────────────┘     └────────────┘     └────────────┘
         │                  │                   │
         ▼                  ▼                   ▼
   ┌──────────────────────────────────────────────┐
   │  5 层 Markdown 文件 + journal 目录            │
   │  MEMORY.md / SELF.md / PENDING.md            │
   │  HISTORY.md / RECENT_CONTEXT.md              │
   └──────────────────────────────────────────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  Consolidation    │
                    │  → 每轮对话后提取   │
                    │  Optimizer 定时归档│
                    └──────────────────┘
```

**实现方式**：
- **Markdown 层**（人类可读）：5 个文件 + journal 目录
  - `MEMORY.md` — 事实性长期记忆（全文注入 system prompt）
  - `SELF.md` — agent 自我认知画像
  - `PENDING.md` — 待归档缓冲
  - `HISTORY.md` — 时间线事件
  - `RECENT_CONTEXT.md` — 近期上下文摘要
- **向量层**：`memory2.db`（sqlite-vec 语义检索）
- **插件层**：Akasha 引擎（图关系 + 密集检索 + 重放）
- **consolidation** 在每轮对话后异步执行
- **Optimizer** 定时将 PENDING → MEMORY.md

### 记忆系统对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **存储方式** | SQLite 单库（表结构化） | Markdown 文件 + 向量 DB + 插件引擎 |
| **人类可读性** | ❌ 数据库，需工具查看 | ✅ Markdown 文件可直接阅读和编辑 |
| **检索时机** | Phase 3（回合开始时） | 每回合自动注入 + 可选语义检索 |
| **知识提取** | Phase 6 提取候选，异步写入 | 对话后 consolidation + Optimizer 定时归档 |
| **去重** | CandidateDeduplicator | DedupDecider + 语义指纹 |
| **用户画像** | UserProfile 模型 | SELF.md + ProfileExtractor |
| **持久化频率** | 每回合原子提交 | 每回合 + 定时优化 |
| **向量存储** | SQLite 内嵌 float 数组 | sqlite-vec 扩展 |
| **嵌入模型** | OpenAI-compatible embedding API | 任意 OpenAI-compatible API |
| **检索增强** | HyDE（无原生实现） | HyDE Enhancer + Query Rewriter |
| **图记忆** | ❌ 不支持 | ✅ Akasha 插件支持 |

---

## 5. LLM 集成

### Cogito v1

**架构**：
```
LLMService → ModelRegistry → ChatBackend(Adapter+Client)
                                ├── OpenAI 适配器
                                ├── DeepSeek 适配器
                                ├── DashScope 适配器
                                └── OpenAI Compatible 适配器
```

**特点**：
- 适配器模式，4 种后端
- `ModelRegistry` 管理模型路由（主/视觉/嵌入）
- `ModelPort` 抽象（LLMServiceModelPort 适配）
- 流式支持在 `cogito/llm/stream.py`
- 模型能力声明（`ModelCapabilities`）

### Akashic Agent

**架构**：
```
LLMProvider (OpenAI-compatible AsyncOpenAI)
  ├── main 模型（推理主力）
  ├── fast 模型（轻量决策：gate/rewrite/HyDE）
  └── vl 模型（视觉多模态）
```

**特点**：
- 单一 `LLMProvider` 类，通过配置区分模型角色
- `AsyncOpenAI` 客户端，兼容任意 OpenAI API
- 原生支持 `thinking` 标签解析（DeepSeek 风格）
- 专门处理 `ContentSafetyError` 和 `ContextLengthError`
- 轻量/重型模型职责分离（gate → 轻量，推理 → 主力）

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **后端多样性** | 4 种适配器 | 1 种（OpenAI-compatible） |
| **适配器模式** | ✅ 完整 | ❌ 无 |
| **模型路由** | ModelRegistry | 配置区分角色 |
| **流式解析** | StreamProcessor | 原生 asyncio 迭代 |
| **Thinking 标签** | ❌ 无 | ✅ 原生解析 |
| **错误重试** | ChatBackend 内 | HttpRequester 层 |
| **嵌入** | EmbeddingPort 抽象 | memory2 内嵌 |
| **视觉支持** | VisionTool (Phase 5) | 内置 VL 工具 |
| **厂商限制** | 无 | 推荐 DeepSeek + Qwen |

---

## 6. 工具系统

### Cogito v1 —— 严格分层的工具系统

```
ToolOrchestrator ───→ ToolSelector ───→ ToolValidator
                          │
                     ToolRegistry
                     (AtomicToolRegistry)
                          │
                     ToolHandler (17+ 内置)
                     ├── ReadFileHandler
                     ├── WriteFileHandler
                     ├── EditFileHandler
                     ├── ShellHandler
                     ├── WebFetchHandler / WebSearchHandler
                     ├── GlobSearchHandler / GrepSearchHandler
                     ├── RecallMemoryHandler / MemorizeHandler
                     ├── SendMessageHandler
                     ├── SpawnHandler
                     └── ...
                          │
                    ┌─────┴─────┐
               ToolPolicy  Sandbox
               (审批/审计)   (安全规则)
```

**特点**：
- **多层治理**：政策引擎 → 沙箱 → 速率限制器 → 审计 → 检查点
- **沙箱安全**：20+ 危险命令模式（YAML 配置）、文件路径保护、网络策略、Shell 规避防护
- **审批流程**：支持人工批准/拒绝/暂停
- **工具注册**：`AtomicToolRegistry` + 冲突策略（SKIP/REPLACE/ERROR）
- **重复守卫**：含 `RepetitionGuard` 防止重复调用
- **MCP 支持**：`infrastructure/mcp/` 有完整客户端

### Akashic Agent —— 灵活实用的工具系统

```
ToolRegistry (中心注册表)
    │
    ├── 标准工具
    │   ├── WebSearchTool (Exa MCP)
    │   ├── WebFetchTool (markdown 转换)
    │   ├── ShellTool
    │   ├── SpawnTool (子 Agent)
    │   ├── ScheduleTool
    │   ├── VisionTool
    │   ├── MemorizeTool / RecallMemoryTool / ForgetMemoryTool
    │   ├── MessageLookupTool / MessagePushTool
    │   ├── ToolSearchTool (运行时发现)
    │   └── Filesystem工具
    │
    ├── MCP 工具 (动态注册)
    │
    ├── Peer Agent 工具 (A2A 协议)
    │
    └── 插件 @tool 装饰器注册
```

**特点**：
- **单注册表**：`ToolRegistry` 维护所有工具
- **ToolSearchTool**：LLM 可动态搜索和启用工具（运行时发现）
- **@tool 装饰器**：插件可通过装饰器快速注册工具
- **ToolHook 链**：pre/post 执行钩子
- **ToolExecutor**：执行工具并管理结果
- **Spawntool**：生成子 Agent 执行并行任务
- **Peer Agent**：通过 A2A 协议调用其他 agent 的工具

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **工具数量** | 17+ | 15+ |
| **注册机制** | AtomicToolRegistry + 冲突策略 | ToolRegistry + @tool 装饰器 |
| **沙箱/安全** | ⭐⭐⭐⭐⭐ (沙箱+策略+审计+限流) | ⭐⭐⭐ (shell_safety 插件) |
| **审批流程** | ✅ 完整（暂停/批准/拒绝） | ✅ 插件方式（tool_loop_guard） |
| **MCP** | 内置 MCP 客户端 | MCP 客户端 + 注册表 |
| **运行时发现** | ❌ 无 | ✅ ToolSearchTool |
| **Peer Agent** | ❌ 无 | ✅ A2A 协议 |
| **子 Agent** | ✅ SpawnHandler | ✅ SpawnTool |
| **工具分类** | 单层目录 | meta/register 分类（Read/Write/Automation） |
| **Hook 机制** | ToolHook | ToolHook（更灵活） |
| **重试策略** | PersistenceRetryPolicy | ToolExecutor 支持 |

---

## 7. 插件/扩展系统

### Cogito v1 —— 通过端口替换实现扩展

Cogito **没有**传统的插件系统。扩展通过以下方式实现：

1. **端口/适配器**：替换 `infrastructure/` 中的实现
2. **策略注入**：替换 `ports/` 中的策略对象
3. **阶段替代**：更换 `RuntimePhase` 实现（需修改 bootstrap）
4. **事件订阅**：通过 `DomainEventBus` 监听事件
5. **Bootstrap 替换**：替换 `bootstrap/` 中的工厂函数

```python
# 示例：替换检索适配器
kernel = build_runtime_kernel(
    retrieval_sources=[
        MyCustomRetriever(),  # 实现 RetrieverPort 即可
    ]
)
```

### Akashic Agent —— 完整的插件系统

Akashic 有 **4 种插件介入方式**：

1. **PhaseModule**：在 7 个 Phase 中插入 Module 链
   ```python
   class MyModule(PhaseModule):
       slot_dependencies = ["memory"]  # 声明依赖
       async def handle(self, ctx: BeforeTurnCtx):
           ctx.skill_names.append("my-skill")
   ```

2. **EventBus 装饰器**：监听 9 种生命周期事件
   ```python
   @plugin.on(TurnStarted)
   async def on_turn_started(self, event: TurnStarted):
       logger.info(f"Turn started: {event.session_key}")
   ```

3. **`@on_tool_pre`**：拦截工具调用
   ```python
   @plugin.on_tool_pre("shell")
   async def before_shell(self, ctx: BeforeToolCallCtx):
       if "rm -rf" in ctx.arguments.get("command", ""):
           ctx.block(reason="Dangerous command")
   ```

4. **`@tool` 装饰器**：注册自定义工具
   ```python
   @plugin.tool("my_custom_tool", "Do something custom")
   async def my_tool(self, args: dict) -> str:
       return "Done!"
   ```

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **插件系统** | ❌ 无（端口替换策略） | ✅ 完整插件系统 |
| **插件数量（内置）** | N/A | 13 个 |
| **安装方式** | 改 bootstrap 代码 | 自动发现 `plugins/` 目录 |
| **生命周期钩子** | AgentEvent 订阅 | PhaseModule + EventBus |
| **工具拦截** | ToolPolicyPort | @on_tool_pre |
| **依赖管理** | N/A | Slot 依赖声明 + 拓扑排序 |
| **热插拔** | ❌ 需重启 | ❌ 需重启 |
| **插件优先级** | N/A | 拓扑排序（支持 before/after 声明） |

---

## 8. 通信频道

### Cogito v1

```
ChannelRegistry (频道注册表)
    │
    └── 当前实现：
        └── WebChannel (AsyncWebServer)
            ├── 内嵌 HTML/CSS/JS 聊天 UI
            ├── HTTP API 路由
            └── Session 管理
        
    计划中：CLI / QQ / 微信 / Telegram
```

**设计**：
- 频道通过 `Channel` 协议定义
- 入站消息 → `InboundBus`（asyncio.Queue）→ `TurnRunner`
- 出站消息 → `DeliveryManager`
- Web 频道是纯 `asyncio` 实现，无外部依赖

### Akashic Agent

```
infra/channels/
    ├── TelegramChannel (python-telegram-bot)
    ├── QQChannel (NapCat / ncatbot)
    ├── QQBotChannel (官方 QQ Bot API)
    ├── CLI (text-based client)
    ├── CLI_TUI (Textual 框架)
    └── IPCServer (Unix socket / TCP)
```

**设计**：
- `Channel` 基类提供公共协议
- 通过 `bootstrap/channels.py` 启动
- IPC 服务器允许 CLI 客户端连接运行中的 agent
- 支持群组消息过滤（`GroupFilter`）
- Telegram 有专门的重启/重连机制

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **当前频道数** | 1（Web） | 5（Telegram/QQ/QQBot/CLI/IPC） |
| **频道抽象** | Channel protocol | Channel 基类 |
| **Web UI** | 内嵌纯 asyncio | React Dashboard（:2236） |
| **外部依赖** | 无（纯 asyncio） | python-telegram-bot / ncatbot |
| **IPC** | ❌ 无 | ✅ Unix socket / TCP |
| **群组过滤** | ❌ 无 | ✅ GroupFilter |
| **CLI TUI** | ❌ 无 | ✅ Textual 框架 |

---

## 9. 主动推送与后台任务

### Cogito v1 —— 无主动机制

Cogito v1 当前**只支持被动回复**模式。没有：
- ❌ 主动推送管道
- ❌ 定时轮询机制
- ❌ 后台任务系统
- ❌ Drift/idle 任务

所有功能都是 "收到消息 → 处理 → 回复" 的请求-响应模式。

### Akashic Agent —— 完整的三层主动系统

```
Proactive V2 System
    │
    ├── Energy Model (电量模型)
    │   └── 自适应轮询频率：刚聊完 8min/次 → 空闲 1min/次
    │
    ├── Sensor Framework (传感器框架)
    │   └── MCP 数据源轮询 → alert/content/context 三路数据
    │
    ├── LLM Judge (决策引擎)
    │   └── 评分分类 → "推"或"不推"
    │
    └── Drift System (空闲任务)
        └── SKILL.md 定义的后台任务
            ├── 记忆审计
            ├── 用户画像构建
            ├── 自我诊断
            └── (可扩展)
```

**Proactive 流程**：
1. 电量模型决定是否轮询
2. Gateway 拉取三路 MCP 数据（alert/content/context）
3. LLM Judge 判断每条内容：是否相关/有趣/值得推送
4. 有内容 → 推送；无内容 → 进入 Drift
5. Drift 执行 SKILL.md 定义的任务

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **主动推送** | ❌ 不支持 | ✅ 完整（V2） |
| **后台任务** | ❌ 不支持 | ✅ Drift 系统 |
| **定时任务** | ❌ 不支持 | ✅ SchedulerService + ScheduleTool |
| **电量管理** | N/A | ✅ Energy 模型 |
| **SKILL.md** | ❌ 无 | ✅ 8 个内置技能文件 |
| **MCP 数据源** | MCP 客户端存在但未用于轮询 | ✅ 用于 proactive 数据采集 |

---

## 10. 配置系统

### Cogito v1

```
cogito/config/
    ├── schema.py      — Pydantic 配置模型
    ├── loader.py      — TOML 文件加载器
    ├── errors.py      — 配置错误类型
    └── __init__.py
```

**方式**：Pydantic 数据类验证
**文件**：`config/config.toml`
**功能**：
- TOML 格式，环境变量展开（`${VAR}`）
- 多 LLM 供应商配置
- 结构化验证（Pydantic）

### Akashic Agent

```
agent/
    ├── config.py        — TOML 加载器
    └── config_models.py — 配置数据类

config.example.toml
```

**方式**：Python dataclass + toml 库
**文件**：`config.toml`（由 `setup` 命令生成）
**功能**：
- `${ENV_VAR}` 插值
- 预设 URL 解析（DeepSeek/Qwen/OpenAI）
- 窗口路径归一化
- 时区验证

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **验证方式** | Pydantic（自动验证） | 纯 dataclass（手动验证） |
| **配置生成** | 手动编写 | `setup` 交互向导 + `init` 非交互 |
| **API Key 管理** | 明文（开发期） | 明文 + 环境变量 |
| **多 LLM 支持** | ✅ 4 种后端 | ✅ 推荐 DeepSeek+Qwen |
| **频道配置** | 基础 | 详细（Telegram/QQ/QQBot） |
| **Proactive 配置** | ❌ 无 | ✅ 详细配置 |

---

## 11. 数据持久化

### Cogito v1

```
数据库：SQLite (aiosqlite)
模式版本：v4
表：
  ├── trace_events  — 追踪/跨度数据
  ├── events        — 事件时间线
  ├── memories      — 记忆 + embedding
  └── FTS5 索引     — 全文搜索

迁移：run_migrations()（v1→v4）
事务：SQLiteUnitOfWork（6 个仓库的原子边界）
```

### Akashic Agent

```
数据库：SQLite (SQLAlchemy) + sqlite-vec
内存引擎：
  ├── memory2/memorizer.py    — 核心记忆逻辑
  ├── memory2/retriever.py    — 多策略检索
  ├── memory2/store.py        — 向量存储（sqlite-vec）
  └── memory2/dedup_decider.py — 语义去重

Markdown 文件系统：
  ├── MEMORY.md               — 人类可读长期记忆
  ├── SELF.md                 — 自我画像
  ├── PENDING.md              — 待归档
  ├── HISTORY.md              — 时间线
  └── RECENT_CONTEXT.md       — 近期上下文

JSON 持久化：
  └── json_store.py           — 配置/状态持久化

Session 存储：
  ├── session/manager.py      — Session 管理器
  └── session/store.py        — SQLite 会话存储（含 FTS5）
```

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **SQL 库** | aiosqlite（原生 asyncio） | SQLAlchemy（同步包装） |
| **向量扩展** | ❌ 不支持 | ✅ sqlite-vec |
| **全文搜索** | FTS5 | FTS5（session + memory2） |
| **迁移系统** | ✅ 自定义迁移（v1→v4） | ❌ 无正式迁移 |
| **事务支持** | ✅ UnitOfWork | ❌ 无统一事务 |
| **Markdown 持久化** | ❌ 无 | ✅ 核心功能 |
| **嵌入式向量** | 手动 float 数组 | sqlite-vec 原生向量 |
| **Session 存储** | 待实现 | ✅ 完整实现 |

---

## 12. 安全与沙箱

这是 Cogito 相比 Akashic 最显著的优势领域。

### Cogito v1 —— 企业级安全

```
cogito/infrastructure/sandbox/
    ├── command_policy.py       — 命令规则引擎
    ├── file_path_guardian.py   — 文件路径保护
    ├── network_policy.py       — 网络策略（SSRF 防护）
    ├── sandbox_impl.py         — 沙箱实现
    ├── rule_engine.py          — 规则引擎
    ├── shell_evasion_guardian.py — Shell 规避防护
    ├── secret_redactor.py      — 密钥脱敏
    ├── workspace_scope.py      — 工作区范围限定
    └── rules/
        └── dangerous_commands.yaml — 20+ 危险命令模式
```

**规则示例**（YAML）：
```yaml
- pattern: "rm -rf /"
  severity: CRITICAL
  action: BLOCK
  message: "禁止删除根目录"
```

### Akashic Agent —— 插件级安全

```
plugins/shell_safety/       — Shell 命令安全防护
plugins/tool_loop_guard/    — 工具循环防护
plugins/shell_restore/      — Shell 会话恢复
```

**特点**：
- 安全作为**插件**实现，非核心层
- `shell_safety` 插件拦截危险 shell 命令
- `tool_loop_guard` 防止工具死循环
- 无中央策略引擎或 YAML 规则文件

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **沙箱** | ✅ 完整实现 | ❌ 无 |
| **命令策略** | 规则引擎 + YAML 规则 | shell_safety 插件 |
| **文件路径保护** | ✅ FilePathGuardian | ❌ 无 |
| **网络策略** | ✅ SSRF 防护 | ❌ 无 |
| **Shell 规避** | ✅ 专用检测 | ❌ 无 |
| **密钥脱敏** | ✅ SecretRedactor | ❌ 无 |
| **工作区限定** | ✅ WorkspaceScope | ❌ 约定 |
| **工具循环防护** | ✅ RepetitionGuard | ✅ tool_loop_guard 插件 |
| **审批流程** | ✅ 完整 | ✅ 插件 |
| **审计日志** | ✅ 工具审计 + TraceEvents | ✅ observe 插件 |

---

## 13. 测试策略

### Cogito v1

```
tests/
├── agent/           — 核心 agent 测试
│   ├── application/
│   ├── architecture/  — 依赖边界测试
│   ├── ports/
│   ├── retrieval/
│   ├── runtime/       — 包含全部 8 个阶段的测试
│   └── tools/
├── application/
├── bootstrap/
├── bus/
├── config/
├── database/
├── infrastructure/sqlite/ — 持久化层测试
├── llm/
├── security/          — 安全测试
└── turns/
```

**风格**：
- 纯 pytest + asyncio（asyncio_mode=auto）
- 架构测试：验证层间依赖方向
- 端口测试：mock 基础设施
- 集成测试：真实 SQLite + 端到端管道

**测试数据**：~422 个测试（PersistencePhase 完成时数据）

### Akashic Agent

```
tests/
├── 80+ 个测试文件
├── agent_core_p1~p7 系列（按功能渐进）
├── 每个插件独立的测试文件
├── proactive_v2/      — 10 个测试文件
├── memory2/           — 记忆系统测试
├── turns/
└── fixtures/plugins/  — 10 个测试用插件
```

**风格**：
- pytest + asyncio
- 按功能渐进式测试（p1~p7 系列）
- 插件测试使用 fixture 插件模拟
- 有 eval 基准测试（LongMemEval / PersonaMem）

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **测试框架** | pytest | pytest |
| **测试数量** | ~422+ | 估计 300+ |
| **架构测试** | ✅ 依赖边界测试 | ❌ 无 |
| **安全测试** | ✅ 专门 security/ 目录 | ⚠️ 零散 |
| **基准测试** | ❌ 无 | ✅ LongMemEval / PersonaMem |
| **测试用插件** | N/A | 10 个 fixture 插件 |
| **Mock 风格** | 端口抽象 → 轻松 mock | 需 mock 具体实现 |
| **CI** | ❌ 无 | ✅ GitHub Actions (pyright + pytest) |

---

## 14. 技术栈与依赖

### Cogito v1 —— 极简依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `openai` | >=1.0.0 | LLM API 客户端 |
| `pydantic` | >=2.0.0 | 配置验证 + 数据模型 |
| `httpx` | >=0.27.0 | HTTP 客户端 |
| `aiosqlite` | >=0.20.0 | 异步 SQLite |
| **总运行时依赖** | **4 个** | |

构建：setuptools

### Akashic Agent —— 丰富生态

| 类别 | 依赖 | 用途 |
|------|------|------|
| **LLM** | `openai`, `anthropic` | LLM API |
| **HTTP** | `httpx[socks]`, `curl_cffi` | HTTP 请求 |
| **Web** | `fastapi`, `uvicorn`, `starlette` | Dashboard API |
| **通道** | `python-telegram-bot`, `ncatbot` | 通信频道 |
| **记忆** | `sqlite-vec`, `scikit-learn`, `scipy`, `numpy` | 向量/语义检索 |
| **文本** | `jieba`, `beautifulsoup4`, `lxml`, `html2text`, `markdown-it-py`, `ftfy` | 文本处理 |
| **CLI** | `click`, `rich`, `textual` | 命令行/TUI |
| **调度** | `APScheduler`, `schedule`, `tzlocal` | 任务调度 |
| **其他** | `Pillow`, `yt-dlp`, `websockets`, `aiofiles`, `PyYAML`, `toml`, `json_repair`, `fake-useragent`, `duckduckgo_search`, `telegramify-markdown` | |
| **总运行时依赖** | **45+ 个** | |

前端：Node.js + React 19 + TypeScript + esbuild

### 对比

| 对比项 | Cogito v1 | Akashic Agent |
|--------|-----------|---------------|
| **运行时依赖数** | **4**（极简） | **45+**（丰富） |
| **安装时间** | 秒级 | 分钟级 |
| **包大小** | 小 | 大 |
| **前端技术栈** | 纯 Python（内嵌） | React 19 + TypeScript |
| **构建系统** | setuptools | setuptools + esbuild |
| **静态类型检查** | 无 | ✅ pyright（严格模式） |
| **代码格式化** | 无 | ✅ black |
| **CI/CD** | ❌ 无 | ✅ GitHub Actions |
| **包管理** | pip | uv（推荐） |

---

## 15. 代码规模与项目健康度

| 指标 | Cogito v1 | Akashic Agent |
|------|-----------|---------------|
| **Python 源文件数** | ~358 个 | ~280+ 个 |
| **测试文件数** | ~60+ 个 | ~80+ 个 |
| **插件数** | 0（端口替换策略） | 13 个内置插件 |
| **设计文档** | 18 篇（中文规范） | 6 篇 _handbook + 各插件 README |
| **CI 覆盖** | ❌ 无 | ✅ GitHub Actions |
| **类型检查** | ❌ 无 | ✅ pyright（严格模式） |
| **代码格式化** | ❌ 无 | ✅ black |
| **开发工具** | 无 | VS Code 配置 + Conda |
| **包管理** | pip | uv/requirements.txt |
| **架构图** | 文字描述 | ASCII 架构图 |
| **社区** | 无 | QQ 交流群 |
| **入口数量** | 1（cogito CLI） | 5+ 子命令 |

---

## 16. 关键设计决策对比

### 16.1 框架 vs 产品

```python
# Cogito 思维：提供可替换的抽象
class ModelPort(ABC):
    """你来实现这个接口，我用你的实现"""

# Akashic 思维：提供可直接运行的配置
config.llm.main.model = "deepseek-v4-flash"
config.llm.fast.model = "qwen-flash"
```

### 16.2 固定管道 vs 插件生命周期

```python
# Cogito：阶段顺序硬编码，可靠但缺乏灵活性
async def run(self, request):
    for phase in self._phases:   # 注入时确定
        await phase.run(ctx)

# Akashic：Phase 内部 Module 链动态组合
class BeforeTurnFrame(Phase):
    @property
    def module_chain(self) -> list[type[PhaseModule]]:
        return topological_sort(self._modules)  # 运行时排序
```

### 16.3 数据库 vs 文件系统记忆

```
# Cogito：所有数据在 SQLite 中，结构一致但不可直接编辑
.workspace/cogito.db → memories 表

# Akashic：Markdown 文件可直接编辑，但一致性更难保证
~/.akashic/workspace/
  ├── MEMORY.md       # "写给我自己看" 的记忆
  ├── SELF.md         # "我是谁" 的自我描述
  ├── PENDING.md      # "待整理" 的草稿
  └── memory2.db      # "机器搜索用" 的向量库
```

### 16.4 严格分层 vs 实用组合

```python
# Cogito：层间依赖必须遵守 DDD 规则（甚至用架构测试验证）
# 从 tests/agent/architecture/test_dependency_boundaries.py
def test_domain_does_not_import_infrastructure():
    ...

# Akashic：没有正式的分层约束
# from agent.core.passive_turn import ...  # 直接导入
```

### 16.5 安全深度

```
# Cogito：安全是基础设施层的一等公民
infrastructure/sandbox/ → command_policy / network_policy / ...

# Akashic：安全是插件关注点
plugins/shell_safety/   # 一个插件 ≈ 一种保护
```

### 16.6 扩展性哲学

```
Cogito：    替换端口 → 改变行为
Akashic：   写个插件 → 改变行为

Cogito：
    优点：类型安全、编译时验证、架构文档化
    代价：需要更多样板代码、修改需重启

Akashic：
    优点：快速添加功能、社区贡献门槛低
    代价：运行时不确定性、隐式依赖
```

### 16.7 异步模式

```python
# Cogito：纯 asyncio，从 SQLite 到 HTTP 全异步
import aiosqlite
async with aiosqlite.connect(...) as db:
    ...

# Akashic：混合模式
# memory2.db 使用 SQLAlchemy（同步，用线程池包装）
session = Session()
results = session.execute(query)
```

---

## 17. 总结与建议

### Cogito v1 的优势

1. **架构质量**：DDD 分层清晰，端口/适配器分离使测试和替换都非常容易
2. **安全深度**：沙箱系统最突出——命令规则引擎、路径保护、网络策略、Shell 规避防护等等，几乎覆盖所有攻击面
3. **依赖极简**：只有 4 个运行时依赖，安装快，易于集成到现有项目
4. **事务性持久化**：UnitOfWork + 13 步原子写入，数据一致性有保障
5. **中文设计文档**：18 篇详细规范，文档化做得很好
6. **架构测试**：验证层间依赖方向，防止架构退化

### Akashic Agent 的优势

1. **功能丰富度**：主动推送 + Drift 后台任务 + 定时调度，远超 Cogito
2. **插件系统**：4 种介入方式 + 13 个内置插件，扩展性极好
3. **开箱即用**：配置好就能跑，有 `setup` 交互向导
4. **记忆系统**：Markdown 文件人类可读可编辑，向量层语义检索，双层设计实用
5. **多频道支持**：Telegram/QQ/QQBot/CLI/IPC，真实可用的 bot
6. **社区与 CI**：GitHub Actions + QQ 交流群，项目健康度高

### 可能的融合方向

1. **Cogito 可以借鉴的**
   - **插件系统**：Cogito 的端口替换策略是"硬编码"级别的扩展，引入轻量插件系统可以让三方贡献更简单
   - **Proactive + Drift**：这是目前 Cogito 完全没有的能力，但这是 AI agent 差异化竞争力的关键
   - **Markdown 记忆**：让数据结构化 + 人类可读并存
   - **频道多样性**：Telegram 和 QQ 是真实用户需求的渠道

2. **Akashic 可以借鉴的**
   - **沙箱安全**：Akashic 的 shell 安全只有一层插件防护，Cogito 的多层沙箱架构值得参考
   - **DDD 分层**：Akashic 的代码组织偏实用，长期维护可能会遇到耦合问题
   - **架构测试**：添加依赖方向验证可以防止架构退化
   - **事务一致性**：没有 UnitOfWork，数据一致性靠代码约定保证

### 定位差异化建议

| 如果... | 选择... |
|---------|---------|
| 你想构建一个安全、可靠、可嵌入的 AI agent 框架 | **Cogito v1** |
| 你想部署一个真正能用的、会和用户主动聊天的 AI 伙伴 | **Akashic Agent** |
| 你的项目需要严格的合规/安全审计 | **Cogito v1** |
| 你希望快速迭代、实验新功能 | **Akashic Agent** |
| 你的团队熟悉 DDD 和架构驱动开发 | **Cogito v1** |
| 你的团队希望低门槛贡献插件 | **Akashic Agent** |

**最终结论**：两个项目定位互补而非竞争。Cogito v1 像**引擎**——它可以驱动任何 AI 产品，但需要调试和装配；Akashic Agent 更像**整车**——它已经装好轮子、导航系统，加满油就能上路。如果目标是学习 AI agent 架构设计，两个项目都值得深入研究。
