# 10：一次性破坏性 Cutover 迁移

## 目标

在可验证、可恢复的停机窗口内，把既有 SQLite/Payload 数据导入不可变 Event，再原子替换为 Event-only 数据库。

## 前置条件

- 阶段 09 完成，Event-only schema 回归通过。
- API、Worker、Scheduler、Connector 已全部停止；操作进程可以取得 SQLite 独占锁。
- 数据库和 Payload 根目录位于可备份、可恢复的路径；候选库与目标库可使用同文件系统原子替换。

## 实施步骤

1. 获取独占锁后创建 SQLite 原子快照和 Payload 备份；验证备份文件内容 hash，不能只 hash 路径字符串。
2. 在候选副本运行非破坏性 schema 升级，创建 `event_log` 与索引。
3. 用 `LegacyEventBackfill` 以确定性 Event ID 导入每类旧实体为 `legacy.<entity>.imported`：保留原始时间、实体 ID、最终状态、关联 ID、摘要、payload ref/hash；不虚构中间生命周期。
4. 对候选库验证实体数量、最终状态、关联关系、payload ref/hash、每个导入流的版本与 `PRAGMA integrity_check`。
5. 验证通过后删除完整旧状态表和索引，写入包含真实备份内容 hash 的 cutover marker，再次验证 Event-only runtime readiness。
6. 仅在所有验证成功时原子替换原数据库；任何异常删除候选副本、保留原库和备份，并返回可读失败报告。

## 测试与退出条件

覆盖空库、代表性导入库、导入中断、payload hash 不匹配、锁竞争、删表失败、原子替换失败和备份恢复。Cutover 后启动检查必须在 marker 缺失、旧表残留或 Event 表缺失时拒绝处理工作。
