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
```

### 通过公开 Python API 运行

Cogito 不再提供 `cogito` CLI（参见 `plan/09_架构收敛与可发布闭环开发计划.md` PLAN-09 / M0）。
所有启动与调试通过 `cogito.application.RuntimeApplication` 公开装配根完成：

```python
# run.py — 你自己的部署/启动脚本
import asyncio
from pathlib import Path
from cogito.config import Config
from cogito.application import RuntimeApplication

config = Config.load(Path("config.toml"))
app = RuntimeApplication.build(config)
try:
    # 单次交互（调试/测试用）
    reply = asyncio.run(app.process_terminal_message("你好"))
    print("[bot]", reply)

    # 或者，后台 worker 模式（长期运行）
    # asyncio.run(app.run_worker("worker1", poll_interval=1.0))
finally:
    app.close()
```

更多示例见 `examples/`（后续 PR 补齐）。

## interaction-web 仪表盘（API + 前端）

提供 Query/Command API（对齐架构文档 `ACCESS-DELIVERY §2.2/2.3`）、WebSocket 聊天，
并托管 React 仪表盘的静态产物。**所有数据库读写均经服务层，handler 不直接操作 SQLite。**

通过公开 Python API 启动：

```python
# serve.py
import asyncio
from pathlib import Path
from cogito.config import Config
from cogito.application import RuntimeApplication
from cogito.interaction_web.server import create_app

config = Config.load(Path("config.toml"))
# 可选覆盖端口
config.interaction.port = 8081

rt = RuntimeApplication.build(config)
asyncio.run(rt.start_web_channel())

import uvicorn
app = create_app(
    config,
    recovery_counts=rt.recovery_counts(),
    static_dir=Path(config.workspace_path) / "web" / "dist",
    runtime=rt,
)
uvicorn.run(app, host=config.interaction.bind_host, port=config.interaction.port)
```

> Worker 也可用 `asyncio.run(rt.run_worker("web-worker", config.worker.heartbeat_interval_seconds))`
> 后台启动；具体示例见 `examples/serve.py`（后续 PR 补齐）。

```powershell
python -m pytest tests/interaction_web/ -q
```

### 前端开发（Vite + React + TS）

```powershell
cd web
npm install
npm run dev                        # 开发模式，5173 端口，/api 代理到 8081
npm run build                      # 构建产物输出到 .workspace/web/dist（由 serve 托管）
```

## 质量门禁

```powershell
python -m pytest -q                  # 应全绿
python -m pytest -q tests/channel/test_qq_onebot_contract.py  # QQ OneBot Contract
python -m pytest -q tests/integration/test_qq_onebot_e2e.py   # QQ OneBot E2E
python -m ruff check                  # 应全绿（legacy channel/adapters|vendor|clients|utils 已隔离）
python -m compileall -q src           # 应无输出（全绿）
```

当前基线（截至 PLAN-12 M6）：**Ruff 0 errors（新代码），compileall clean，Pytest 全绿（1211 passed）**。
QQ OneBot Channel: **experimental**（自动门禁通过，真实 QQ 人工验收待完成）。

## 实现状态

| 模块 | 状态 | 说明 |
|---|---|---|
| 领域实体 | ✅ 完成 | Principal、Conversation、Session、Message、Turn、Task、Delivery、MemoryItem |
| 状态机 | ✅ 完成 | Turn、RunAttempt、Task、Delivery、Memory 状态转移验证 |
| 异常层次 | ✅ 完成 | 实体未找到、非法状态转移、并发冲突、幂等违反、Lease 错误等 |
| SQLite 存储 | ✅ 完成 | Schema、连接管理、编号 Migration（含 Gateway Receipt 与 Plugin 生命周期），INTEGER epoch ms 时间 |
| CLI | ❌ 已移除 (PLAN-09/M0) | 原 `python -m cogito` 已删除；改为公开 Python API (`RuntimeApplication`) |
| 严格配置 | ✅ 完成 | ConfigError 单行可操作提示、secret 不泄漏、未知字段显式报错 |
| 入站事务 | ✅ 完成 | accept_inbound（Inbox 幂等、Conversation/Session/Message/Turn/Outbox 同事务） |
| Dispatcher + Lane | ✅ 完成 | 按优先级 DESC 调度、Lane 隔离、原子 RunAttempt 创建 |
| Outbox / Delivery Worker | ✅ 完成 | Lease/版本校验、指数退避重试、dead-letter |
| Delivery Receipt | ✅ 完成 | confirmed/uncertain/reconciled/temporary/permanent 持久凭证 |
| Delivery Service / Gateway | ✅ 完成 | 单一 SqliteDeliveryService；Loopback/HTTP 双部署；send/edit/finish/delete/reconcile/health；unknown 先对账 |
| Recovery Service | ✅ 完成 | 过期 Lease 回收（sending→unknown）、stale Turn 清理、**流式孤儿 Delivery 撤回（streaming→interrupted）与 Turn 重放（recover_streaming_deliveries）**；启动时自动运行 |
| Stub / OpenAI-compatible Provider | ✅ 完成 | 流式 + 非流式调用；`run_stream` 增量生成；缺省配置自动降级为 Stub；DeepSeek-R1/LongCat 类模型的 `reasoning_content` 回退 |
| Model Router | ✅ 完成 | 按角色路由、有限重试、fallback |
| Context Builder | ✅ 完成 | 不可变 ContextSnapshot、Session 隔离、Token 估算 |
| Agent Loop | ✅ 完成 | FinalResponse/Refusal/InvalidOutput 修复、终止条件守卫 |
| Orchestrator | ✅ 完成 | Dispatcher→Context→AgentLoop→TurnCompletion 集成 |
| Tool Registry / Executor | ✅ 完成 | 内置工具发现、Toolset 按 mode 启用 |
| MCP Client / Manager | ✅ 完成 | 官方 `mcp` SDK 实现 stdio + SSE 传输；外部 MCP 按配置验收 |
| Plugin Runtime | ✅ 完成 | SQLite 状态、subprocess 宿主监督、权限 Grant、熔断、Audit、升级快照与回滚 |
| Memory / Summary | ✅ MVP | 长期记忆 + FTS5 + Embedding 检索 + 生命周期管理 |
| Multimodal Perception | ✅ image MVP | Asset/Payload/Store、VisionAnalysis 缓存、可重试 Durable Task、Context 注入、`analyze_multimodal_asset` Tool、Vision 指标（requested/cache_hit/started/completed/failed/latency）；`[multimodal]` 默认关闭 |
| RuntimeApplication | ✅ 完成 | 统一装配根（SQLite + migrate + recover + Provider + Runner + Channel 组件） |
| 外部 Channel Adapter | ⚠️ 实验性 | 17 个 Adapter 已注册但未经逐个可导入性验证 |
| QQ OneBot Channel | ⚠️ experimental | aiocqhttp 1.4.4 + LangBot Facade，自动 E2E 通过，真实 QQ 待验收 |
| 流式 Channel 投递 | ✅ 完成 | placeholder → 增量 edit → 最终定稿（is_final）；支持 edit 的 Channel 走 `StreamingDeliveryController`，由 Turn 的 RunAttempt Lease 拥有（不经过 DeliveryWorker），崩溃后经 `recover_streaming_deliveries` 撤回并重放，Web 订阅时清理遗留占位气泡 |
| LangBot Bridge | ✅ 闭环 | 执行真实 Gateway 操作；operation key 持久去重；Loopback/HTTP 共用 GatewayClient Port |
| Web Dashboard / API | ✅ 完成 | Query/Command API + WebSocket 聊天；FastAPI 真实接口 + React 前端（Overview/Chat/Runs/Tasks/Proactive/Deliveries/Connectors/Memory/Capabilities/Trace/Audit/System 共 12 页） |
| Web Channel（聊天） | ✅ 完成 | `WebChannelAdapter` 注册进 `ChannelManager`；浏览器消息经 `InboundService.accept(web envelope)` 进 Core 主链路，回复经 `ChannelGateway` 路由回 web adapter 队列由 WS 实时推回（与 QQ/Terminal 对称，Core 零改动） |

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
├── application.py     # RuntimeApplication — 统一装配根（唯一）
├── config.py          # 严格分层配置 + ConfigError
├── model/             # 模型契约、Provider 抽象、Stub Provider、Router
├── runtime/           # Clock、Context Builder、Agent Loop（拟纯协议层）
├── service/           # Dispatcher、Completion、Workers、Recovery
├── domain/            # 领域实体与状态机
├── store/             # 存储、Migration、Repository、time_utils
├── contracts/         # 入站消息契约
├── capability/        # Tool Registry / Executor / MCP
├── tools/             # 内置工具
├── inbound/           # 入站 Dispatcher
├── infrastructure/    # Backup / Restore / PayloadStore / Runbook / Profile
└── channel/
    ├── base.py        # ChannelAdapter Protocol + 结构化 DTO
    ├── bridge.py      # LangBot Bridge 契约适配
    ├── bridge_server.py # LangBot Bridge HTTP 路由
    ├── manager.py     # ChannelManager 渠道注册与路由
    ├── drivers/       # 纳入 lint 门禁的 Adapter（qq_onebot、onebot_models）
    └── adapters/      # Legacy Adapter（隔离 lint）
```

### QQ OneBot 11 渠道

需要 `pip install -e ".[qq]"` 安装 aiocqhttp 1.4.4。配置示例见 `config.example.toml` [channel.qq] 节。

```powershell
pip install -e ".[qq]"
# 复制并编辑配置
Copy-Item config.example.toml config.toml
# 取消 [channel.qq] 注释、填入你的 QQ owner 和 access_token
# 通过公开 Python API 启动（见上面的 run.py / serve.py 示例）
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

## 架构文档索引

- 文档索引：`markdown/00_guides/00_文档索引与Agent阅读指南.md`（入口）
- 权威索引：`manifest.json`
- 系统边界：`markdown/01_architecture/03_系统边界与依赖规则.md`
- 当前依赖图：`docs/architecture/current-dependency-map.md`
- 开发计划：`plan/`（含 PLAN-01 ~ PLAN-12，全部 completed）

## 架构门禁

4 阶段 CI（push/PR to `master`）：

1. architecture（`tests/architecture/`，含依赖规则扫描、循环依赖检测）
2. quality（ruff + compileall）
3. test（全局 pytest）
4. recovery（`pytest -m recovery`）

已在执行的架构例外登记：`tests/architecture/test_dependency_rules.py`
中的 `KNOWN_VIOLATIONS`。每条例外都有 `adr_link` 与 `clear_by` 截止日；到
期未清零测试会失败。

## 许可证

MIT
