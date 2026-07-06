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
```

## 实现状态

当前实现覆盖以下内容：

| 模块 | 状态 | 说明 |
|---|---|---|
| 领域实体 | ✅ 完成 | Principal、Conversation、Session、Message、Turn、Task、Delivery、MemoryItem |
| 状态机 | ✅ 完成 | Turn、RunAttempt、Task、Delivery、Memory 状态转移验证 |
| 异常层次 | ✅ 完成 | 实体未找到、非法状态转移、并发冲突、幂等违反等 |
| SQLite 存储 | ✅ 完成 | Schema、连接管理、版本 Migration |
| CLI | ✅ 基本 | `init` 创建 workspace 和数据库，`info` 显示系统信息 |
| 入站事务 | ⏳ 待实现 | accept_inbound（P2 阶段） |

详细开发计划见 `plan/` 目录。

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

配置文件为 `config.toml`，支持 `database` 和 `workspace` 两个主要配置段：

```toml
[database]
path = "data/cogito.db"
enable_wal = true
busy_timeout = 5000

[workspace]
path = ".workspace"
payload_dir = "data/payload"
log_dir = "logs"
```

## 许可证

MIT
