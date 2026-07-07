# Cogito — 主动式个人 Agent

Cogito 是一个**本地优先、单所有者的主动式个人 Agent** 架构知识库与可运行基线实现。

## 仓库定位

本仓库包含两部分内容：

1. **架构知识库** — 分层设计文档（`markdown/`），入口 `markdown/00_guides/00_文档索引与Agent阅读指南.md`
2. **可运行基线** — Python 实现（`src/cogito/`），当前版本 `0.1.0-alpha.1`

代码入口：`src/cogito/`

## 快速开始（Windows PowerShell 推荐路径）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item config.example.toml config.toml
python -m cogito config check          # 校验配置（exit 0 表示通过）
python -m cogito info --config config.toml
python -m cogito run --interactive
```

不带模型配置时默认使用 Stub Provider（固定文本回复），无需网络；真实模型在
`config.toml` 中 uncomment 并填入 `[model.main]` 节即可启用。

退出 REPL：输入 `/quit`、`/exit` 或 `/q`。

## 质量门禁

```powershell
python -m pytest -q                  # 应全绿
python -m pytest -q tests/channel/test_qq_onebot_contract.py  # QQ OneBot Contract
python -m pytest -q tests/integration/test_qq_onebot_e2e.py   # QQ OneBot E2E
python -m ruff check                  # 应全绿（legacy channel/adapters|vendor|clients|utils 已隔离）
python -m compileall -q src           # 应无输出（全绿）
python -m cogito config check --config config.example.toml
python -m cogito config check --config tests/fixtures/config/qq_onebot.toml  # QQ enabled
```

当前基线：**678 tests passed in ~34s**，Ruff 0 errors，compileall clean。
QQ OneBot Channel: **experimental**（自动门禁通过，真实 QQ 人工验收待完成）。

## 实现状态

| 模块 | 状态 | 说明 |
|---|---|---|
| 领域实体 | ✅ 完成 | Principal、Conversation、Session、Message、Turn、Task、Delivery、MemoryItem |
| 状态机 | ✅ 完成 | Turn、RunAttempt、Task、Delivery、Memory 状态转移验证 |
| 异常层次 | ✅ 完成 | 实体未找到、非法状态转移、并发冲突、幂等违反、Lease 错误等 |
| SQLite 存储 | ✅ 完成 | Schema、连接管理、编号 Migration（v1-v20），INTEGER epoch ms 时间 |
| CLI | ✅ 完成 | `config check / init / info / run / memory`；共享 `--config` 参数 |
| 严格配置 | ✅ 完成 | ConfigError 单行可操作提示、secret 不泄漏、未知字段显式报错 |
| 入站事务 | ✅ 完成 | accept_inbound（Inbox 幂等、Conversation/Session/Message/Turn/Outbox 同事务） |
| Dispatcher + Lane | ✅ 完成 | 按优先级 DESC 调度、Lane 隔离、原子 RunAttempt 创建 |
| Outbox / Delivery Worker | ✅ 完成 | Lease/版本校验、指数退避重试、dead-letter |
| Delivery Receipt | ✅ 完成 | confirmed/uncertain/reconciled/temporary/permanent 持久凭证 |
| Recovery Service | ✅ 完成 | 过期 Lease 回收（sending→unknown）、stale Turn 清理；启动时自动运行 |
| Stub / OpenAI-compatible Provider | ✅ 完成 | 非流式调用；缺省配置自动降级为 Stub |
| Model Router | ✅ 完成 | 按角色路由、有限重试、fallback |
| Context Builder | ✅ 完成 | 不可变 ContextSnapshot、Session 隔离、Token 估算 |
| Agent Loop | ✅ 完成 | FinalResponse/Refusal/InvalidOutput 修复、终止条件守卫 |
| Orchestrator | ✅ 完成 | Dispatcher→Context→AgentLoop→TurnCompletion 集成 |
| Tool Registry / Executor | ✅ 完成 | 内置工具发现、Toolset 按 mode 启用 |
| MCP Client / Manager | ✅ 骨架 | 外部 MCP 仍需按配置验收 |
| Memory / Summary | ✅ MVP | 长期记忆 + FTS5 + Embedding 检索 + 生命周期管理 |
| Terminal Channel | ✅ 完成 | 可运行基线（本 PR） |
| RuntimeApplication | ✅ 完成 | 统一装配根（SQLite + migrate + recover + Provider + Runner） |
| 外部 Channel Adapter | ⚠️ 实验性 | 17 个 Adapter 已注册但未经逐个可导入性验证 |
| QQ OneBot Channel | ⚠️ experimental | aiocqhttp 1.4.4 + LangBot Facade，自动 E2E 通过，真实 QQ 待验收 |
| 流式 Channel 投递 | ⏳ 待实现 | 后续 PR |
| LangBot Bridge | ⏳ 待实现 | 后续 PR |
| Web Dashboard / API | ⏳ 待实现 | 后续 PR |

## 架构概要

```
interaction-web → agent-api ↔ agent-worker → sqlite + payloads
                         ↕
                 channel-gateway (LangBot)
                         ↕
                 Model Adapter (Contracts/Router/Provider)
                         ↕
               model / MCP / external services
```

三类运行模型：**Turn Orchestrator**（即时交互 + Model Adapter）、**Durable Job Runner**（长期工作）、**Event Bus**（事实传播）。

## 模块结构

```
src/cogito/
├── application.py     # RuntimeApplication — 统一装配根
├── __main__.py        # CLI 入口（参数解析 + 退出码转换）
├── config.py          # 严格分层配置 + ConfigError
├── model/             # 模型契约、Provider 抽象、Stub Provider、Router
├── runtime/           # Clock、Context Builder、Agent Loop
├── service/           # Dispatcher、Completion、Workers、Recovery
├── domain/            # 领域实体与状态机
├── store/             # 存储、Migration、Repository、time_utils
├── contracts/         # 入站消息契约
├── capability/        # Tool Registry / Executor / MCP
├── tools/             # 内置工具
├── inbound/           # 入站 Dispatcher
└── channel/
    ├── base.py        # ChannelAdapter Protocol + 结构化 DTO
    ├── drivers/       # 纳入 lint 门禁的 Adapter（qq_onebot、onebot_models）
    ├── adapters/      # Legacy Adapter（隔离 lint）
    └── vendor/        # LangBot 兼容层（隔离 lint）
```

### QQ OneBot 11 渠道

需要 `pip install -e ".[qq]"` 安装 aiocqhttp 1.4.4。配置示例见 `config.example.toml` [channel.qq] 节。

```powershell
pip install -e ".[qq]"
# 复制并编辑配置
Copy-Item config.example.toml config.toml
# 取消 [channel.qq] 注释、填入你的 QQ owner 和 access_token
python -m cogito config check
python -m cogito run
```

详见 `docs/operations/qq-onebot.md`。

## 项目配置

配置文件为 `config.toml`，支持分层配置：

```toml
workspace_path = ".workspace"

[storage]
db_path = "data/cogito.db"
enable_wal = true
busy_timeout = 5000
payload_dir = "data/payload"
```

配置校验：`cogito config check --config <path>`。

## 许可证

MIT
