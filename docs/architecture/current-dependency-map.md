# 当前模块依赖图（架构基线）

> Plan 01 M1 交付物 · 自动生成 + 人工审计  
> 扫描时间：2026-07-10 · 范围：`src/cogito/` 顶层子包间导入  
> 设计依据：`SYSTEM-BOUNDARIES / 2. 模块依赖方向`

## 1. 模块依赖图

扫描规则：对每个 `src/cogito/<subpackage>/*.py`，收集其 `from cogito.X import` / `import cogito.X` 语句，映射到顶层 `cogito.<subpackage>`。

```
domain           -> (none)                          ★ 纯领域层，无依赖 (符合)
contracts        -> (none)                          ★ 纯契约层，无依赖 (符合)
config           -> (none)                          ★ 纯配置解析，无依赖 (符合)
bench            -> (none)

store            -> contracts, domain
model            -> config, contracts
capability       -> contracts, store
tools            -> capability, contracts
runtime          -> capability, contracts, model
inbound          -> contracts, service
channel          -> config, contracts
service          -> bench, capability, channel, config, contracts,
                    domain, model, runtime, store  → Application Service，宽依赖受测试约束
interaction_web  -> bench, channel, config, contracts, service
application      -> capability, channel, config, contracts, inbound,
                    model, service, store, tools       → 装配根，允许
```
> PLAN-09 M0 已移除 `__main__.py`（CLI 入口）。部署脚本应直接调用
> `cogito.application.RuntimeApplication`，不再经过 CLI。

## 2. 期望的依赖方向（SYSTEM-BOUNDARIES / 2）

```text
domain ← application ← adapters/infrastructure
                   ↑
              plugin public API
```

禁止（`SYSTEM-BOUNDARIES / 2`）：

- Domain 导入数据库、HTTP、模型 SDK 或 Channel SDK；
- Agent Runtime 直接写 Repository；
- Event Handler 绕过 Command 修改其他聚合；
- Plugin 导入 Core 私有模块或核心 ORM；
- Dashboard 直接执行写 SQL。

## 3. 违规清单

当前 `KNOWN_VIOLATIONS` 为空；V1～V8、C1～C2 均已清零。CI 继续以
`test_no_new_forbidden_edges` 和 `test_no_import_cycles` 阻止回归。新增例外必须
先有 accepted ADR 和到期日，不能通过修改本图掩盖扫描结果。

## 4. 公开面（各聚合唯一写入入口, SYSTEM-BOUNDARIES / 4）

| 状态 | 期望唯一写入者 | 当前 Facade 状态 |
|---|---|---|
| Conversation/Session | IdentityConversationService | Phase 1 M2 定义 |
| Turn/RunAttempt | TurnService | 已有 Protocol 基础 |
| Task/TaskAttempt | TaskService | 已有 Protocol 基础 |
| MemoryItem | MemoryService | 已有 Protocol 基础 |
| Delivery | DeliveryService | `SqliteDeliveryService` 唯一实现，Worker/Task 共享 |
| Approval | ApprovalService | Phase 1 M2 定义 |
| Plugin 状态 | PluginRuntime | `SqlitePluginRuntime` 唯一实现，Command 经 Runtime |

## 5. 装配根合法依赖

`application.py` 作为唯一装配根，允许依赖全部子包。业务模块不得反向依赖装配根（PLAN-09 §3.2）。

## 6. 自动扫描脚本

`tests/architecture/_scan.py` 无第三方依赖（stdlib `ast`），CI 中复现本图；本图应与扫描输出一致，偏差需在 PR 中说明。
