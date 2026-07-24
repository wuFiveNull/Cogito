# 09：删除旧状态、兼容代码与旧测试

## 目标

将阶段 04–08 的双路径彻底收口，确保任何正式运行时在旧表不存在时仍可启动和工作。

## 实施步骤

1. 从 Dispatcher、Repository、Task/Scheduler、Connector、Memory、Knowledge、Drift、Delegation、Query、Command 和 Recovery 中删除 legacy fallback、旧 SQL 和旧 DTO。
2. 删除 Outbox Worker、旧 Event Publisher、Trace Repository、旧 Delivery Worker、Reconcile Service 及不再可达的配置开关。
3. 将所有测试 fixture 改为 Event 构造器和 PayloadStore；断言 replay projection、消费者输出或 API 响应，禁止插入/更新旧表。
4. 将旧表 SQL 静态扫描升级为 CI 阻断规则。唯一允许项为 legacy importer、cutover 工具、旧 migration 文件和针对迁移的隔离测试。
5. 新建 Event-only 测试 schema：保留 `event_log`、Payload 元数据、schema/配置元数据，刻意省略所有旧业务表；在该 schema 上跑后端集成与 Web E2E。

## 退出条件

- 生产源码旧表依赖扫描为零，允许清单没有运行时模块。
- Event-only schema 下全量后端和前端回归通过。
- 任何“旧 Event 流为空就查旧行”的逻辑均已删除。
- 阶段完成前不得执行真实数据库 cutover。
