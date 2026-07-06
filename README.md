# Cogito — 主动式个人 Agent 框架

Cogito 是一个**本地优先、单所有者的主动式个人 Agent** 架构知识库与早期实现。

## 仓库定位

本仓库包含两部分内容：

1. **架构知识库** — 35 份中文分层设计文档（`markdown/`）
2. **早期实现** — 基于 Pydantic 的 Python 代码（`src/cogito/`）

架构文档入口：`markdown/00_guides/00_文档索引与Agent阅读指南.md`
代码入口：`src/cogito/`

## 快速开始

```bash
# 安装依赖
pip install -e .

# 初始化工作区和数据库
python -m cogito init

# 查看系统信息
python -m cogito info

# 运行测试
python -m pytest

# 代码检查
python -m ruff check src tests

# 编译检查
python -m compileall -q src
```

## 实现状态

| 模块 | 状态 | 说明 |
|---|---|---|
| 领域实体 | ✅ 完成 | Principal、Conversation、Session、Message、Turn、Task、Delivery、MemoryItem、DeliveryReceipt |
| 状态机 | ✅ 完成 | Turn、RunAttempt、Task、Delivery、Memory 状态转移验证 |
| 异常层次 | ✅ 完成 | 实体未找到、非法状态转移、并发冲突、幂等违反、Lease 错误等 |
| SQLite 存储 | ✅ 完成 | Schema、连接管理、编号 Migration（v1-v9），INTEGER epoch ms 时间 |
| CLI | ✅ 完成 | `init` 创建 workspace 和数据库，`info` 显示系统信息（无 API Key 仍可用） |
| 严格配置 | ✅ 完成 | 分层配置模型（runtime/storage/interaction/worker 等），未知字段报错 |
| 入站事务 | ✅ 完成 | accept_inbound（Inbox 幂等、Conversation/Session/Message/Turn/Outbox 同事务） |
| Dispatcher + Lane | ✅ 完成 | 按优先级 DESC 调度、Lane 隔离、原子 RunAttempt 创建，Lease 强制执行 |
| Stub TurnCompletion | ✅ 完成 | 固定回复、原子写入 Message + Delivery + Outbox |
| Outbox Worker | ✅ 完成 | 聚合顺序、Lease/版本校验、指数退避重试、精确 dead_letter，TTL 配置 |
| Delivery Worker | ✅ 完成 | Lease/版本校验、失败重试、unknown→reconcile、精确 failed，TTL 配置 |
| Delivery Receipt | ✅ 完成 | confirmed/uncertain/reconciled 持久凭证，Migration 0008 |
| Recovery Service | ✅ 完成 | 过期 Lease 回收（sending→unknown）、stale Turn 清理（验证 Lease 有效性） |
| Clock 接口 | ✅ 完成 | ProductionClock、FakeClock（advance()），所有 Service 统一时间源 |
| RunAttempt Lease | ✅ 完成 | worker_id/lease_version/lease_expires_at/heartbeat_at，全流程强制校验 |
| 数据库时间工具 | ✅ 完成 | epoch_ms/from_epoch_ms 工具，所有 INTEGER 列不写 ISO 字符串 |
| 可靠性测试矩阵 | ✅ 完成 | 故障窗口、Lease 边界、Recovery 竞态、v5/v6 升级、Migration FK 检查 |
| 模型统一契约 | ✅ 完成 | ModelRequest/Response、Usage、Capabilities、ErrorEnvelope、FinishReason |
| Model Provider | ✅ 完成 | Provider Protocol、StubModelProvider（确定性预设序列） |
| Model Router | ✅ 完成 | 按角色路由、有限重试、fallback、调用记录回调 |
| ModelCall 持久化 | ✅ 完成 | model_calls 表（Migration 0009），Router 回调自动记录 |
| Context Builder | ✅ 完成 | 不可变 ContextSnapshot、Session 隔离、Token 估算、超限裁剪 |
| Agent Loop | ✅ 完成 | FinalResponse/Refusal/InvalidOutput 修复、终止条件守卫 |
| Orchestrator | ✅ 完成 | Dispatcher→Context→AgentLoop→TurnCompletion 集成，心跳、Lease 再验证 |
| OpenAI-compatible Provider | ⏳ 待实现 | PR 11 独立交付 |
| Tool/MCP/Plugin | ⏳ 待实现 | 当前阶段不启用 |
| Memory/Gol/Summary | ⏳ 待实现 | Context Builder 保留接口 |
| 流式 Channel 投递 | ⏳ 待实现 | 后续 PR |
| LangBot Bridge | ⏳ 待实现 | 后续 PR |

**当前运行模式：** 使用 Stub Provider（确定性文本回复）完成端到端 Turn 执行链验证。
生产环境需接入真实 Model Provider（计划 PR 11）。

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

当前 Turn Orchestrator 使用 **Stub Provider** 完成确定性验证，接入真实 Model Provider 见 PR 11。

## 模块结构

```
src/cogito/
├── model/             # 模型契约、Provider 抽象、Stub Provider、Router
├── runtime/           # Clock、Context Builder、Agent Loop、Orchestrator
├── service/           # Dispatcher、Completion、Workers、Recovery
├── domain/            # 领域实体与状态机
├── store/             # 存储、Migration、Repository、time_utils
├── contracts/         # 入站消息契约
└── config.py          # 配置模型
```

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

## 许可证

MIT
