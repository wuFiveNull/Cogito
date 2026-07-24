# 01：基线、旧依赖清单与架构护栏

## 目标

建立可持续维护的旧状态表依赖清单，并让 CI 阻止新的生产路径回到旧表。该阶段不删除任何旧表。

## 当前已知缺口

现有代码已经包含 `EventStore`、`event_replay`、`event_catalog` 和部分 Event-first 服务，但 `dispatcher.py`、`task_repo.py`、`schedule_repo.py`、`connector_repo.py`、`query_service.py`、`command_service.py`、`delegation_lifecycle.py`、Drift/Connector/Knowledge 服务仍存在旧表 SQL。测试也仍大量通过插入 `turns`、`tasks`、`deliveries` 构造状态。

## 实施步骤

1. 建立一份机器可读的旧表名单：交互、Turn/Attempt、Task/Attempt、Delivery、Outbox、Trace、Connector、Schedule、Memory、Knowledge、Proactive、Drift、Approval、Command、Audit、Ingestion Batch。
2. 为每张表记录四个字段：目标聚合流、目标 replay 函数、legacy importer 事件名、计划删除阶段。
3. 在架构测试中扫描 `src/cogito` 的 SQL 字符串。允许项仅为 migration、`legacy_event_backfill.py`、`event_store_cutover.py` 和专门的兼容删除测试；允许项必须显式列出文件，不得用目录通配放宽。
4. 将所有命中按“已 Event 化但残留 fallback”“双路径”“纯旧表”分类；每完成一个阶段更新清单。
5. 给每个纯旧表模块分配到后续阶段；没有目标 Event 和 replay 的表不能进入 cutover 清单。

## 测试与退出条件

- 架构测试能检测任何新增的旧业务表 SQL。
- 清单覆盖每一张计划删除表及其所有生产访问点。
- 每张表均具备 Event 流、导入策略和删除阶段。
- 此阶段不要求旧表扫描为零；要求扫描结果可解释且无未归属项。
