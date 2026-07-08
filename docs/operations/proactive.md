# 主动推送（Proactive Push）操作手册

> 遵循 Plan 06 §16 发布与回滚。

## 快速启用

1. 修改 `config.toml`：
   ```toml
   [proactive]
   enabled = true            # 开启主动推送 worker
   dry_run = true            # 第 1–3 步保持 dry-run
   default_principal_id = "owner"
   minimum_relevance = 0.55
   minimum_novelty = 0.60
   same_topic_cooldown_minutes = 360
   max_pushes_per_hour = 3
   max_pushes_per_day = 10
   digest_max_delay_minutes = 360
   candidate_ttl_hours = 48

   [proactive.quiet_hours]
   enabled = true
   start = "23:00"
   end = "08:00"
   timezone = "Asia/Shanghai"
   ```

2. 重启 cogito。观察日志 `[proactive-evaluate]` 输出决策。

3. 确认 dry-run 结果符合预期后，翻转：
   ```toml
   [proactive]
   dry_run = false
   ```
   重启。

## Kill Switch（无需数据库回滚）

任意一条单独或组合使用：

| 关闭级别 | 操作 | 影响 |
|---|---|---|
| 完全关闭 | `proactive.enabled = false` | 停止创建 evaluate/candidate/delivery |
| 仅观测 | `proactive.dry_run = true` | 仍跑决策路径，不创建 Delivery |
| 单一 MCP 源 | 对应 `[capability.mcp.servers.<name>]` 的 `enabled = false` | 该 Connector 停止拉取，不影响其它源 |
| 单一 Channel | 对应 channel 的 `enabled = false` | 阻止新 Delivery 选择该 Endpoint |
| 单 Principal | 修改 DB `proactive_policies.dry_run=1` | 仅该 principal 进入观测 |

## 控制面

- 默认 **三路分类**：`alert` / `content` / `context`。`alert` 走即时通道，`content` 默认进 digest（最大延迟 `digest_max_delay_minutes`），`context` 不主动推送。
- **冷却**：同 topic 冷却分钟（默认 6h）使用最后一次**实际发送**时间。
- **预算**：每 hour / 每 day 发送上限；超出降级为 `send_later` 或 `digest`。
- **安静时段**：跨午夜自动处理（23:00–08:00）。
- **能量模型**：三档衰减（30m/4m/48h），仅调整 urgency 权重 + novelty/relevance 阈值。

## 数据流

```
[connector.poll / mcp_connector.poll Task]
    → MCP/RSS 摄取 → SourceEvent (Outbox)
        → SourceEventIngested → ProactiveCandidate
            → [proactive.evaluate Task] → decide()
                ├ send_now → Delivery (TargetSnapshot 固定)
                ├ send_later → scheduled_delivery_request + delivery.ready Task
                └ digest → scheduled_digest + digest.publish Task → markdown → Delivery
```

## 回滚

- Migration 采用 expand-only；代码回滚时新表保留但不被读取。
- Outbox 事件默认保留，不会物理删除；错误 Candidate 通过状态/新 Event 撤销。

## 完成检查

| 检查项 | 状态 |
|---|---|
| 全量测试通过 | ✅ 865 passed / 0 failed |
| 真实 MCP dry-run 冒烟 | ✅ |
| dry-run 无真实 Delivery | ✅ |
| 全链路可追溯 (event→candidate→decision→delivery receipt) | ✅ |
