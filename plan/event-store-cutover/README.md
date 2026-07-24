# Event Store 改造执行手册

## 目标

将 `event_log` 设为 Cogito 唯一的持久化应用事实源。业务状态、任务待办、投递恢复、审计时间线和 Trace 均由不可变 Event 重放得到。Payload 存储只保留受权限控制的原始内容；它不是业务状态表。进程内缓存可以存在，但必须可随时丢弃并由 Event/Payload 重建。

## AI 阅读顺序

每次开始一个阶段前，必须依次阅读：

1. [00-execution-rules.md](00-execution-rules.md)
2. 当前阶段文档；
3. 当前阶段的直接依赖阶段文档；
4. 受影响模块的 `AGENTS.md` 指定架构文档、源码和测试。

不得仅凭本手册中的示例推断当前实现；先检索当前代码并以实际契约为准。

## 阶段依赖

```text
01 基线与护栏
        ↓
02 Event 契约与 Store
        ↓
03 回放、消费者与副作用
        ↓
04 交互运行时与投递 ────┐
05 Task、Scheduler、Delegation ─┤
        ↓                         ↓
06 Connector、Memory、Knowledge  07 Proactive、Drift、Multimodal
        └───────────────┬─────────┘
                        ↓
08 API、Web 与 Event Explorer
                        ↓
09 删除旧状态与兼容路径
                        ↓
10 破坏性 Cutover 迁移
                        ↓
11 验证、发布与验收
```

阶段 04 与 05 可以并行实施，但都依赖阶段 03。阶段 06 和 07 只能在各自依赖的 Task/Delivery 流稳定后开始。阶段 10 只能在阶段 09 的 Event-only 数据库测试通过后执行。

## 全局完成门槛

- 生产运行时不读取或写入旧业务状态表、`outbox_events`、旧 `events`、`traces` 或 `spans`。
- 空库与导入库重放得到相同的聚合状态、待办任务、未终结副作用和因果树。
- 任一用户请求可使用一个 `trace_id` 查询完整生命周期。
- Event 永久保存；Payload 过期后仍保留摘要、hash、引用及失效语义。
- Cutover 失败时原数据库与 Payload 备份可恢复，应用拒绝在半切换状态启动。

## 统一阶段状态

每个阶段在交接时标记为：`not_started`、`in_progress`、`blocked` 或 `complete`。只有满足该文件的退出条件且记录证据后才可标记 `complete`。
