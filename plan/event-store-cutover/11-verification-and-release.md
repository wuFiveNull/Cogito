# 11：验证、发布与最终验收

## 回归矩阵

执行并记录以下类别：

- Event/Store 单元测试：Catalog、版本、幂等、Payload 安全、序列化。
- Replay/Consumer 测试：重复回放、重复消费、并发竞争、未知副作用、回执恢复。
- 运行时集成：入站→回复、模型/工具失败、审批、流式投递、任务恢复、Connector→Memory/Knowledge、Proactive、Drift。
- 迁移测试：备份、导入、验证、删表、失败回滚和启动拒绝。
- API/Web E2E：Event Explorer、时间线、Trace 树、payload 权限、深链和 Event-only schema。

## 对比验证

为“空库新运行”和“旧库导入后运行”分别 replay，比较：

1. 聚合最终状态与流版本；
2. 待执行 Task、过期 lease、未终结 Delivery/Tool/Connector 请求；
3. Approval、Connector cursor、Memory/Knowledge 可见状态；
4. Proactive/Drift 的待办及结果；
5. 由 `trace_id` 重建的 span 树与 causation 边。

重复 replay 或重复恢复后，不得增加外部副作用或终结 Event。

## 发布 Gate

- 全量测试通过，且 Event-only schema 回归通过。
- 旧表 SQL CI 扫描为零。
- 候选 cutover、备份验证和失败恢复演练通过。
- Payload 访问权限、过期语义和 hash 校验通过。
- Event Explorer 可在导入库中查询完整 trace。

## 发布后观测与最终验收

监控 Event 追加失败、版本冲突、消费者滞后、unknown 副作用、payload hash 不匹配和 cutover marker 异常。最终验收要求：任一用户请求可按一个 `trace_id` 重建完整生命周期；所有可见状态均可由 Event 回放恢复；生产运行时不再访问旧业务状态表。
