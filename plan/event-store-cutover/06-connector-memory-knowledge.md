# 06：Connector、Memory、Knowledge 与检索

## 目标

将外部数据摄取、记忆与知识生命周期改为 Event-only，并确保检索索引始终可由 Event/Payload 重建。

## 实施步骤

1. 定义 Connector 流：创建、配置/状态变更、游标变更、poll 请求、source ingestion、batch 开始/完成/失败/重试。现有 `ingestion_batches` 只能作为待删除目标，不能继续作恢复依据。
2. 对每个 source 使用稳定来源 ID、content hash 与 connector cursor 生成幂等键；重复拉取只能追加一次有效 ingestion 结果。
3. Connector 消费者用因果 Event 创建 Task、Memory extraction 和 Knowledge sync；不直接插入任务行。
4. Memory 使用 `memory.extraction.requested`、signal、candidate、confirmed/rejected/superseded/expired/erased/weight 事件；记忆正文只在 PayloadStore。
5. Knowledge 使用资源、解析、分段、embedding、失效和删除事件。若需要全文/向量索引，定义为可删除的本地派生缓存，重建输入仅为 Event 与 Payload。
6. 将 QueryService、知识解析/同步、记忆提取、权重服务和连接器恢复改为 replay 或可重建缓存。

## 验证

测试重复摄取、batch 崩溃、cursor 恢复、Memory 擦除、资源删除、embedding 失败重试、索引全量重建和检索结果一致性。完成后 Connector、Memory、Knowledge 的生产路径不得读取其旧状态表。
