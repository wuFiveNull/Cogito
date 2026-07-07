# QQ OneBot 11 渠道运行手册

> **状态：experimental**
> 自动 E2E 已通过，真实 QQ 人工验收待完成后可升为 beta。

## 架构

```text
NapCat / Lagrange（QQ 客户端）
    │ OneBot 11 reverse WebSocket
    ▼
AiocqhttpAdapter（LangBot 兼容层）
    │ LangBot MessageEvent / MessageChain
    ▼
QQOneBotAdapter（Core ChannelAdapter Facade）
    │ canonical Inbound
    ▼
InboundDispatcher → InboundService → AgentRunner → TurnCompletionService
                                                        │ Assistant Message
                                                        │ Delivery(pending)
                                                        └→ DeliveryWorker → QQOneBotAdapter.send()
                                                                                    │
                                                                                    ▼
                                                                             OneBot send_*_msg
```

## 环境前置

1. **Python 3.12+** 虚拟环境已激活。
2. 安装 Cogito 及 QQ 依赖：
   ```powershell
   pip install -e ".[qq]"
   ```
3. NapCat 或 Lagrange 已登录测试 QQ 账号，并启用 OneBot 11 reverse WebSocket 模式。
4. 目标 URL 只绑定 `127.0.0.1:<port>`（第一版仅允许 loopback）。
5. NapCat/Lagrange 的 access_token 与 Cogito 配置一致。
6. **单独测试 QQ 账号**，不要用主号做首轮测试。

## 配置

```toml
[channel.qq]
enabled = true
driver = "aiocqhttp"
instance_id = "qq-main"
host = "127.0.0.1"
port = 8080
access_token = "${COGITO_QQ_ACCESS_TOKEN}"   # 环境变量引用
owner_qq_ids = ["${COGITO_OWNER_QQ}"]        # Owner QQ 号
allow_private = true
allowed_group_ids = []                       # 空 = 拒绝全部群聊
require_mention_in_group = true
startup_timeout_seconds = 15
```

校验：

```powershell
$env:COGITO_OWNER_QQ = "12345678"
$env:COGITO_QQ_ACCESS_TOKEN = "your-token-here"
python -m cogito config check
```

输出中的 `[ok] channel.qq: enabled (instance=qq-main)` 表示 QQ 渠道已启用。

## 启动与停止

启动 agent：

```powershell
python -m cogito run
```

停止：按 `Ctrl+C`。正常关闭会释放监听端口。

## 私聊测试

1. 用 Owner QQ 号向测试机器人发送一条唯一文本，例如 `"测试 2026-07-07T2200"`。
2. 观察 Cogito 日志：
   - `QQ adapter qq-main started`
   - `Turn completed successfully`
   - `Cycle processed: turn=1 outbox=0 delivery=1 task=0`
3. QQ 端收到 Stub 或真实模型回复。
4. 重复推送同一 OneBot event（相同 message_id），QQ 端**不应收到第二条回复**。

## 群聊测试

1. 在 `allowed_group_ids` 中加入测试群 ID。
2. 在群聊中 @Bot 发送消息。
3. 其他成员发消息（未 @Bot）应不触发。
4. 非 allowlist 群的任何消息不进入 Owner 会话。

## unknown Delivery 人工处理

当 Delivery 因发送后响应丢失进入 `unknown` 状态时：

- 启动时自动 reconcile：如果拿到了真实的 platform_message_id，调用 `get_msg` 对账。
- 没有 message_id 的 unknown Delivery：**不自动重发**，需人工检查日志。

## 日志诊断

结构化计数日志：

```text
channel.qq.start / running / disconnected / stopped / error
channel.qq.inbound.accepted / duplicate / filtered
channel.qq.send.attempt / sent / temporary / permanent / unknown
```

日志不记录原始消息正文、token 或完整 QQ 号。

## 故障排查

| 问题 | 检查项 |
|---|---|
| QQ adapter 启动失败 | host 是否为 loopback；port 是否已被占用；token 是否一致 |
| 入站消息无响应 | 检查 `owner_qq_ids` 配置；检查日志是否有 "not_owner" |
| 群聊无响应 | 检查 `allowed_group_ids` 是否包含群 ID；是否需要 @Bot |
| Delivery 堆积 | 查看 `channel.qq.send.*` 计数；确认 NapCat 连接是否正常 |
| 端口未释放 | 等待 5 秒后重试 `cogito run` |

## 验收证据模板

完成真实 QQ 验收后记录（去敏）：

```text
应用 commit: <hash>
配置 hash（不含 Secret）: <sha256 of config without secrets>
aiocqhttp / NapCat / Lagrange 版本: <version>
测试时间: <ISO8601>
脱敏 channel instance: qq-main
Turn ID / Delivery ID / Attempt ID: <ids>
最终状态: sent / unknown / failed
外部发送次数: <count>
Receipt kind: confirmed / uncertain
```

**不保存**：access_token、完整 QQ 号、真实聊天正文、原始认证 Header。
