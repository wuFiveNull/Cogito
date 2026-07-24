# 08：API、查询与 Web Event Explorer

## 目标

将所有可见状态改为 Event replay 查询，并一次性替换旧 Trace/Outbox 页面与 API。

## API 契约

- `GET /events`：支持时间范围、event type、stream type/id、trace、correlation、causation、主体 ID 过滤；以稳定的 `(occurred_at, event_id)` 游标分页。
- `GET /event-traces/{trace_id}`：返回时间线、span 父子边和跨流 causation 边；缺失 parent 时保留孤儿节点而非丢弃。
- `GET /event-timelines?session_id=...`：按会话返回时间线及最小关联摘要。
- `GET /events/{event_id}/payload`：要求专用权限令牌；不存在为 404、无权限为 403、过期/不可读为 410、hash 不匹配为受控错误。

## 实施步骤

1. 删除旧 `/sessions/{id}/trace`、`/traces/{id}`、`/outbox`、dead-letter 和旧 replay-event 实现；`/events` 路径保留但改为上述新语义。
2. QueryService 只使用 EventStore + replay projection；禁止对旧状态表做回退查询、数量统计或详情拼接。
3. Trace UI 改为统一时间线/因果树，展示摘要、状态、耗时、token、工具和 Delivery 结果；各领域页面跳转到同一 Explorer 并传递过滤条件。
4. 删除旧前端 `SessionTrace`、`OutboxEvent` 类型、mock 和多表 join 逻辑。

## 验证

测试游标无重复/漏项、过滤组合、权限拒绝、payload 过期、同 Trace 多流、深链刷新和 Event-only 数据库页面加载。完成后 Web/API 中不得存在旧 Trace、Outbox 或状态表端点。
