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
| 领域实体 | ✅ 完成 | Principal、Conversation、Session、Message、Turn、Task、Delivery、MemoryItem |
| 状态机 | ✅ 完成 | Turn、RunAttempt、Task、Delivery、Memory 状态转移验证 |
| 异常层次 | ✅ 完成 | 实体未找到、非法状态转移、并发冲突、幂等违反、Lease 错误等 |
| SQLite 存储 | ✅ 完成 | Schema、连接管理、编号 Migration（v1-v5） |
| CLI | ✅ 完成 | `init` 创建 workspace 和数据库，`info` 显示系统信息 |
| 严格配置 | ✅ 完成 | 分层配置模型（runtime/storage/interaction），未知字段报错 |
| 入站事务 | ✅ 完成 | accept_inbound（Inbox 幂等、Conversation/Session/Message/Turn/Outbox 同事务） |
| Dispatcher + Lane | ✅ 完成 | 按优先级 DESC 调度、Lane 隔离、原子 RunAttempt 创建 |
| Stub Agent + TurnCompletion | ✅ 完成 | 固定回复、原子写入 Message + Delivery + Outbox |
| Outbox Worker | ✅ 完成 | 聚合顺序、Lease/版本校验、指数退避重试、精确 dead_letter |
| Delivery Worker | ✅ 完成 | Lease/版本校验、失败重试、unknown→reconcile、精确 failed |
| Recovery Service | ✅ 完成 | 过期 Lease 回收、stale Turn/RunAttempt 重置 |
| 可靠性测试 | ✅ 完成 | 并发领取、Lease 过期、旧 Worker 拒绝、重试时序、恢复幂等 |

## 架构概要

```
interaction-web → agent-api ↔ agent-worker → sqlite + payloads
                         ↕
                 channel-gateway (LangBot)
                         ↕
               model / MCP / external services
```

三类运行模型：**Turn Orchestrator**（即时交互）、**Durable Job Runner**（长期工作）、**Event Bus**（事实传播）。

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
