# 全局不变量验证矩阵（Plan 01 M5）

> Plan 01 M5 交付物 · 设计依据：`GLOBAL-INVARIANTS / 1~7`  
> 每条不变量映射到 invariant_id / doc_id / heading / enforcement_type / enforcement_location / test_id / current_status

## 格式说明

```text
invariant_id      GLOBAL-INVARIANTS 条目编号
doc_id / heading  markdown/01_architecture/02_全局不变量.md 中的原文
enforcement_type  db_constraint | service_logic | architecture_test | contract_test | recovery_test | manual_runbook
enforcement_location  执行位置（代码文件/测试文件）
test_id          自动化测试 ID 或 runbook 引用
current_status    DONE | PARTIAL | TODO
```

## 验证矩阵

| invariant_id | heading | enforcement_type | enforcement_location | test_id | status |
|---|---|---|---|---|---|
| INV-1.1 | SQLite 是唯一事实源 | db_constraint + service_logic | store/repositories.py, store/schema.py | test_schema.py (FK + unique) | DONE |
| INV-1.2 | Payload 只有被 SQLite 引用才属事实 | service_logic | infrastructure/payload_store.py | test_infrastructure.py | DONE |
| INV-1.3 | Message/Event/Receipt/Audit 不可原地改变 | db_constraint + service_logic | domain/message.py domain/events.py | test_contracts.py (frozen) | DONE |
| INV-1.4 | Projection/Embedding/缓存可重建 | service_logic | service/memory_service.py, store/memory_repo.py | test_memory_lifecycle.py (rebuild) | DONE |
| INV-2.1 | 一次意图一个 Turn，多 RunAttempt，无独立 Run | service_logic | domain/turn.py, service/dispatcher.py | test_dispatcher.py | DONE |
| INV-2.2 | 同 Context Partition 最多一个可提交 Turn | db_constraint | store/schema.py (unique session+status) | test_dependency_rules.py | DONE |
| INV-2.3 | 等待时不持有线程/txn/Lane/Lease | service_logic | recovery_decision.py (waiting_user→no lease) | test_recovery_decision.py | DONE |
| INV-2.4 | 外部调用不在 DB tx 内 | service_logic | capability/executor.py, runtime/loop.py | test_tool_execution_chain.py | DONE |
| INV-2.5 | 旧 Lease/Attempt 不得提交 | service_logic | service/dispatcher.py (complete 条件更新) | test_dispatcher.py::test_stale_version | DONE |
| INV-3.1 | 入站/Command/Task/Event/Delivery 均有幂等键 | db_constraint + service_logic | store/schema.py (idempotency_key unique) | test_command_envelope.py | DONE |
| INV-3.2 | 副作用先持久化意图后保存 Receipt | service_logic | capability/executor.py, capability/models.py | test_tool_execution_chain.py | DONE |
| INV-3.3 | unknown 先 reconcile 不自动重试 | service_logic | service/recovery_decision.py, service/reconcile_service.py | test_reconcile.py | DONE |
| INV-3.4 | Event 只表示事实，变更其他聚合用 Command | service_logic | service/event_publisher.py | test_event_outbox.py | DONE |
| INV-4.1 | 不同 Channel/Conversation 不共享短期 Session | service_logic | service/session_resolver.py | test_session_resolver.py (isolation) | DONE |
| INV-4.2 | 长期 Memory 保留来源/Scope/置信度/有效期 | db_constraint | store/schema.py (memory_items) | test_memory_lifecycle.py | DONE |
| INV-4.3 | 模型提取内容先成 Candidate | service_logic | service/memory_service.py (propose→candidate) | test_memory_lifecycle.py | DONE |
| INV-4.4 | Context Snapshot 可解释选择了什么 | service_logic | runtime/context.py (items + provenance) | test_session_resolver.py | DONE |
| INV-5.1 | 模型只能提动作，Policy Engine 决定权限 | service_logic | capability/policy.py (evaluate) | test_tool_execution_chain.py (deny) | DONE |
| INV-5.2 | 外部内容不能提升信任等级 | service_logic + contract | capability/mcp_security.py (external_untrusted) | test_contracts.py (trust_label) | DONE |
| INV-5.3 | Shell/文件/网络用受限 Runtime | config_declaration | capability/sandbox.py (5 Profile) | test_sandbox.py | DONE |
| INV-5.4 | 控制面仅 loopback | service_logic | interaction_web/command_envelope.py (enforce_loopback_only) | test_command_envelope.py | DONE |
| INV-6.1 | 重启先恢复一致性再恢复工作 | service_logic | service/recovery_service.py, infrastructure/backup.py | test_recovery.py (recovery smoke) | DONE |
| INV-6.2 | 自动恢复不增加重复副作用风险 | service_logic | service/reconcile_service.py (no blind retry) | test_reconcile.py | DONE |
| INV-6.3 | 预算/Attempt/审批结果跨重启保留 | db_constraint | store/schema.py (task_attempts/approvals) | test_task_attempt_convergence.py | DONE |
| INV-6.4 | 备份可验证 SQLite/Payload/文件哈希 | service_logic | infrastructure/backup.py (verify + integrity) | test_backup_startup.py | DONE |

## Top 10 高风险不变量（优先自动化，对应 test/architecture/test_invariants.py）

| 排名 | invariant_id | 测试 ID |
|---|---|---|
| 1 | INV-1.1 SQLite 唯一事实源 | test_inv_1_1 |
| 2 | INV-2.2 同 Context Partition 最多一个可提交 Turn | test_inv_2_2 |
| 3 | INV-2.4 外部调用不在 DB tx 内 | test_inv_2_4 |
| 4 | INV-2.5 旧 Lease/Attempt 不提交 | test_inv_2_5 |
| 5 | INV-3.1 幂等键 | test_inv_3_1 |
| 6 | INV-3.2 副作用意图-then-Receipt | test_inv_3_2 |
| 7 | INV-3.3 unknown 先 reconcile | test_inv_3_3 |
| 8 | INV-5.2 外部内容不能提升信任 | test_inv_5_2 |
| 9 | INV-2.3 等待不持有 txn/lane/lease | test_inv_2_3 |
| 10 | INV-5.4 控制面仅 loopback | test_inv_5_4 |
