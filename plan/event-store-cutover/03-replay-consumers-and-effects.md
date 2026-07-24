# 03：回放、消费者与副作用框架

## 目标

所有业务状态由纯 replay 函数重建；所有异步处理由输入 Event 驱动并以新的因果 Event 表达结果。

## 实施步骤

1. 为 Message、Session、Turn、RunAttempt、ModelCall、ToolCall、Approval、Delivery、Task、TaskAttempt、Schedule、Connector、Memory、Knowledge、Candidate、Decision、DriftRun 定义 replay 函数。
2. 每个 replay 明确：初始事件、状态转换、终结事件、无效后继事件、输出 projection 和当前 `stream_version`。非状态 Event 仍必须推进 projection 的流版本。
3. 建立订阅协议：消费者声明输入 Event 集合和消费者名；结果 Event 带输入 Event ID 作为 `causation_id`，并采用稳定幂等键。
4. 将副作用规范化为 `requested → started? → completed|failed|unknown|cancelled`。工具、Delivery、Connector 拉取和任何外部写入均遵守该协议。
5. 恢复器扫描未终结请求和 receipt Event；先查外部幂等键/回执，再追加结果 Event，绝不重放调用本身。
6. 对消费者失败定义策略：可重试错误保留未终结请求；不可重试错误追加失败；无法判定时追加 unknown 并进入回执校验。

## 测试与退出条件

- 同一流重复 replay 没有额外副作用且得到相同 projection。
- 同一输入 Event 被重复消费不会生成两个结果或执行两次外部调用。
- 崩溃发生在请求前、请求后、回执前、结果追加前均可恢复。
- 并发消费者只有一个能成功追加同一因果结果。
- 后续阶段只能通过 replay/订阅框架读取和推进状态。
