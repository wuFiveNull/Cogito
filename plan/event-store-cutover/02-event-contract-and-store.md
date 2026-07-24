# 02：Event 契约、Catalog 与 EventStore

## 目标

把 `Event`、`EventContext`、Event Catalog 与 `EventStore` 固化为唯一持久化契约，使所有后续域都能安全地追加、验证、分页和回放事实。

## 契约决策

- 固定字段：`event_id`、流三元组、`event_type`、`type_version`、`event_class`、`producer`、`occurred_at`。
- 因果字段：`trace_id`、`span_id`、`parent_span_id`、`correlation_id`、`causation_id`。
- 主体字段：actor/principal/conversation/session/turn/attempt/task ID；无值使用空字符串，禁止使用含义不明的 `null` 字符串。
- 时间统一为 UTC epoch milliseconds；ID 使用既有 UUID/稳定业务 ID，不由 replay 临时生成。
- `payload_hash` 使用 SHA-256；payload 已过期时 Event 仍保留 `summary`、hash 和失效的 `payload_ref`。

## 实施步骤

1. 审核 Catalog，补齐 Schedule、Delegation、Ingestion Batch、Knowledge Segment、Embedding、Multimodal 的生命周期事件；未知类型必须在边界拒绝。
2. 为每个事件名指定 `EventClass`、初始事件、允许后继事件及最小属性集合；不要把“进度块”或 token 流加入 Catalog。
3. 保持 `(stream_type, stream_id, stream_version)` 唯一约束；`append_many` 在一个事务中校验全部 Catalog、版本和幂等键后写入。
4. 让 Store 返回可区分的 Catalog、版本冲突、幂等命中和序列化错误；调用方只对冲突重试。
5. 递归校验 attributes，拒绝敏感字段及其嵌套变体；仅 PayloadStore 可接收原文。
6. 完成 payload 读取端点的权限校验、hash 校验、404/410/403 语义和审计 Event。

## 验证

必须覆盖：Catalog 拒绝、序列化往返、嵌套敏感字段、单流冲突、跨流原子回滚、重复幂等追加、过期 payload 和 hash 不匹配。阶段退出后，后续阶段不得绕过 Store 直接写 `event_log`。
