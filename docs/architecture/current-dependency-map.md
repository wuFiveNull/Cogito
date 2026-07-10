# 当前模块依赖图（架构基线）

> Plan 01 M1 交付物 · 自动生成 + 人工审计  
> 扫描时间：2026-07-09 · 范围：`src/cogito/` 顶层子包间导入  
> 设计依据：`SYSTEM-BOUNDARIES / 2. 模块依赖方向`

## 1. 模块依赖图

扫描规则：对每个 `src/cogito/<subpackage>/*.py`，收集其 `from cogito.X import` / `import cogito.X` 语句，映射到顶层 `cogito.<subpackage>`。

```
domain           -> (none)                          ★ 纯领域层，无依赖 (符合)
contracts        -> (none)                          ★ 纯契约层，无依赖 (符合)
config           -> (none)                          ★ 纯配置解析，无依赖 (符合)
bench            -> (none)

store            -> domain, model, runtime          ✗ 见 V3, V7
model            -> store                           ✗ 见 V4
capability       -> store                           ✗ 见 V5
tools            -> capability, service, store      ✗ 见 V6
runtime          -> capability, domain, model, service, store  ✗ 见 V1, V2
inbound          -> contracts, service
channel          -> config, inbound
service          -> bench, capability, channel, config, contracts,
                    domain, model, runtime, store, tools  → 装配层，宽依赖可接受
interaction_web  -> bench, config, contracts, domain, service, store  ✗ 见 V8
application      -> capability, channel, config, contracts, inbound,
                    model, service, store             → 装配根，允许
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

| ID | 调用方 | 被调用方 | 规则 | 类型 | 临时 Port/Facade | 处置 |
|---|---|---|---|---|---|---|
| V1 | `runtime` | `store` | Agent Runtime 直接写 Repository | 禁止依赖 | `Repository` 访问经 `service` 层 Facade | Phase 1.5 收敛 |
| V2 | `runtime` | `service` | 反向依赖（service 也依赖 runtime，构成环） | 循环依赖 | 明确 runtime→service 的 port 边界 | Phase 1.5 拆解 |
| V3 | `store` | `runtime` | 反向依赖 | 循环依赖 | store 不应感知 runtime | Phase 1.5 消除 |
| V4 | `model` | `model` 不应导入 store 之外的基础设施 | 越界依赖 | model 的 Repository 访问经 service | Phase 1.5 收敛 |
| V5 | `capability` | `store` | 能力层直接写仓储 | 越界依赖 | 经 `service` 层 Facade | Phase 1.5 收敛 |
| V6 | `tools` | `store` | 工具层直接写仓储 | 越界依赖 | 经 `service` 层 Facade | Phase 1.5 收敛 |
| V7 | `store` | `model` | 仓储层感知模型适配 | 反向依赖 | store 应只依赖 domain | Phase 1.5 消除 |
| V8 | `interaction_web` | `store` | Dashboard 直接执行写 SQL | 禁止依赖 | 所有写操作经 Command API | Phase 1.5 收敛 |

**循环依赖（同属 Phase 1.5 清零范围）：**

| ID | 循环 | 破环处置 |
|---|---|---|
| C1 | channel → inbound → service → channel | inbound 抽取为 port，channel/service 均经 port 解耦 |
| C2 | service → tools → service | tools 接口下沉为 port，service 经 port 调用 tools |

**决策：** V1~V8 + C1~C2 作为已知基线纳入 `tests/architecture/test_dependency_rules.py` 的「已知违规登记」，在 CI 中用带到期时间（`adr_link` + `clear_by`）的例外标记；后续按 M2 公开面收敛时清零，例外到期未清零则测试失败。每当一条违规清零，对应的注册表条目应立即移除。

## 4. 公开面（各聚合唯一写入入口, SYSTEM-BOUNDARIES / 4）

| 状态 | 期望唯一写入者 | 当前 Facade 状态 |
|---|---|---|
| Conversation/Session | IdentityConversationService | Phase 1 M2 定义 |
| Turn/RunAttempt | TurnService | 已有 Protocol 基础 |
| Task/TaskAttempt | TaskService | 已有 Protocol 基础 |
| MemoryItem | MemoryService | 已有 Protocol 基础 |
| Delivery | DeliveryService | 已有 Protocol 基础 |
| Approval | ApprovalService | Phase 1 M2 定义 |
| Plugin 状态 | PluginRuntime | Phase 1 M2 定义 |

## 5. 装配根合法依赖

`application.py` 作为唯一装配根，允许依赖全部子包。业务模块不得反向依赖装配根（PLAN-09 §3.2）。

## 6. 自动扫描脚本

`tests/architecture/_scan.py` 无第三方依赖（stdlib `ast`），CI 中复现本图；本图应与扫描输出一致，偏差需在 PR 中说明。
