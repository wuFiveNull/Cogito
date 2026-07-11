---
plan_id: "PLAN-14"
title: "Proactive 闭环与安全 Drift 开发计划"
version: "1.0"
status: "completed (M0-M7 核心闭环 + R7-R10 收口；M2 Task C 模型增强显式未做)"
created_at: "2026-07-11"
owner: "Cogito"
scope: "修复现有 Proactive 决策、能量、调度、投递和审计闭环；在统一 Task/Attempt/Lease/Checkpoint/Policy 体系内实现可抢占、默认只读、Skill 驱动的 Drift；建立回放、指标和发布门禁"
depends_on:
  - "DOMAIN-CONTRACTS"
  - "RUNTIME-FLOWS"
  - "EXECUTION-LIFECYCLE"
  - "AGENT-LOOP"
  - "PROACTIVE-TASKS"
  - "TASK-SCHEDULER"
  - "EVENT-OUTBOX"
  - "PROACTIVE-IDLE"
  - "ACCESS-DELIVERY"
  - "APPROVAL-COMMANDS"
  - "CAPABILITY-PLUGINS"
  - "TOOL-SANDBOX"
  - "DATABASE-SCHEMA"
  - "CONFIG-PROFILES"
  - "SECURITY-OBS"
  - "OBSERVABILITY-AUDIT"
  - "TEST-EVALUATION"
---

# PLAN-14：Proactive 闭环与安全 Drift 开发计划

## 1. 计划结论

Cogito 不需要照搬 Akashic 的常驻 `proactive_v2` Agent Loop。现有 Event、Candidate、Task、Policy、Digest、Delivery、Lease 和 Audit 已构成更适合长期运行的可靠底座；改进重点是把尚未接通的信号与执行链补齐，并在同一套持久任务模型内实现 Drift。

目标方案：

```text
Proactive
Source/Event
  → Candidate Projection
  → Model Enrichment（可选、只提供语义信号）
  → Deterministic Policy Decision
  → Delivery | ScheduledDeliveryRequest | Digest | Silent
  → Feedback / Metrics / Replay

Drift
Idle Admission
  → Skill Selection
  → drift.run Task + TaskAttempt + Lease
  → Step Execution + ResourceBudget + Policy
  → Checkpoint / Complete / Waiting / Retry
  → 内部维护结果，或 ProactiveCandidate(origin=drift)
  → 统一 Policy / Delivery
```

实施顺序：

```text
M0 基线冻结与契约修正
→ M1 Proactive 审计与能量闭环
→ M2 自适应调度、Context 路与模型增强
→ M3 Drift 领域契约、Schema 与准入控制
→ M4 Drift Skill 与最小只读执行器
→ M5 Checkpoint、抢占、恢复和 Policy 闭环
→ M6 用户可见结果、Dashboard 与反馈
→ M7 回放评测、故障注入和发布门禁
```

M0～M2 修复当前 Proactive，属于 P0/P1；M3～M5 实现安全 Drift MVP；M6～M7 完成产品化。PLAN-13 与本计划可并行开发，但涉及 Memory Drift Skill 时必须等 PLAN-13 的来源、删除和生命周期契约稳定后再启用真实写入。

## 2. 权威设计依据

实现和评审必须用 `doc_id / heading path` 引用：

- `DOMAIN-CONTRACTS / 1.10 Task`：Drift 是 Task 的运行模式，不新增第二套长期任务聚合。
- `DOMAIN-CONTRACTS / 1.12 Delivery`：用户可见发送独立于推理和后台维护结果。
- `DOMAIN-CONTRACTS / 2.8 Command 契约`：策略、配置和审批变更通过 Command，不直接改表。
- `RUNTIME-FLOWS / 2.5 长期任务`：跨重启工作必须使用 Lease、Attempt、Checkpoint 和恢复决策。
- `RUNTIME-FLOWS / 2.7 主动决策`：主动候选、决策、Digest 与 Delivery 是独立阶段。
- `EXECUTION-LIFECYCLE / 4. Checkpoint` 与 `EXECUTION-LIFECYCLE / 5. 重试、等待与恢复`：恢复只能基于兼容、已持久化的确定性检查点。
- `AGENT-LOOP / 7. Checkpoint`：副作用前写 Checkpoint，预算耗尽必须结构化收尾。
- `PROACTIVE-TASKS / 1. Data & Event`：外部数据先保存来源事实，再进入主动决策。
- `PROACTIVE-TASKS / 2. ProactivePolicy 与 Context 视图`：SQLite Policy 是权威，Markdown 是派生视图。
- `PROACTIVE-TASKS / 3. Proactive Decision`：模型输出是信号，Policy 决定授权和投递动作。
- `TASK-SCHEDULER / 4. Lease`：Lease 丢失后不开始新副作用。
- `TASK-SCHEDULER / 7. Schedule`：Scheduler 只创建到期 Task，不执行业务。
- `TASK-SCHEDULER / 10. 公平性与资源`：即时 Turn 优先，Proactive 与 Drift 使用独立并发池和 aging。
- `EVENT-OUTBOX / Outbox 事务模式`：状态提交与派生事件不能存在双写窗口。
- `PROACTIVE-IDLE / 3. 能量模型`：energy 同时影响 urgency 权重与 tick 间隔，不能绕过 Quiet Hours。
- `PROACTIVE-IDLE / 5. 决策顺序`：安全、去重、相关性、能量、时效、冷却和预算顺序固定。
- `PROACTIVE-IDLE / 9. 空闲判定`：Drift 准入是全局 idle，不等于“本轮没有内容”。
- `PROACTIVE-IDLE / 10. Drift 任务目录`：Drift 使用普通 Task/Checkpoint，默认禁止外部副作用。
- `PROACTIVE-IDLE / 11. 抢占`：新 Turn 到达后停止领取新步骤，在安全点写 Checkpoint。
- `PROACTIVE-IDLE / 12. Dry Run`：模拟运行记录完整 Decision，但不创建真实 Delivery。
- `ACCESS-DELIVERY / 主动通知`：目标在 ready-to-send 时重新选择并固定快照。
- `APPROVAL-COMMANDS / Approval`：高风险 Drift 工作必须转换为可持久审批，而不是模型自行放行。
- `TOOL-SANDBOX / Tool 执行`：路径、网络、Shell 和外部副作用受 Sandbox 与 Policy 双重约束。
- `SECURITY-OBS / 4.1 ResourceBudget`：预算跨 Attempt 累计，恢复不能重置。
- `SECURITY-OBS / 4.3 主动系统资源边界`：Drift 默认只读，资源不足时首先降级。
- `TEST-EVALUATION / 发布门禁`：回放、故障注入、隔离、恢复和预算测试必须可重复。

## 3. 当前基线与问题清单

### 3.1 可复用基线

当前代码已经具备：

- `ProactiveCandidate`、`ProactivePolicy`、`ProactiveDecision` 及 SQLite Repository；
- `proactive.evaluate`、`proactive.delivery.ready`、`proactive.digest.publish` Task Handler；
- alert fast-path、allow/deny、novelty、relevance、energy、Quiet Hours、cooldown、日/小时预算；
- ScheduledDeliveryRequest、Digest、Delivery 幂等闭环；
- Task、TaskAttempt、Lease、Retry、misfire 和 `checkpoint_ref`；
- Agent Loop `ResourceBudget` 和副作用前 Checkpoint；
- Capability Policy、Approval、Tool 风险分级和 Receipt；
- Proactive Candidate/Decision/Digest Dashboard Query；
- `FeedbackDriftController` 的压力/预算判断原型。

这些能力必须复用，不创建 `ProactiveLoop` 第二调度器、不创建 `drift.db` 第二事实源、不让 Drift 直接写 Core 数据库。

### 3.2 P0 缺口

| ID | 缺口 | 当前证据 | 风险 | 里程碑 |
|---|---|---|---|---|
| PA-P0-01 | Decision 总被记录为 dry-run | `proactive_decision.persist_decision()` 固定 `dry_run=True` | 审计与真实发送状态不一致 | M1 |
| PA-P0-02 | energy 未接真实用户活动 | `_handle_proactive_evaluate()` 调 `compute_energy(None)` | 永远按低能量提高主动性 | M1 |
| PA-P0-03 | energy 未驱动下一次调度 | Scheduler 使用固定 proactive tick | 设计中的自适应节奏未实现 | M2 |
| DR-P0-01 | Drift 没有真实 Task Handler | 默认 Registry 无 `drift.*` | 规范无法运行 | M3～M4 |
| DR-P0-02 | 没有全局 idle admission | 仅有 `FeedbackDriftController` 原型 | 可能与 Turn/Delivery/恢复争抢资源 | M3 |
| DR-P0-03 | 没有执行级抢占与恢复闭环 | 无 Drift Attempt/Checkpoint 流程 | 用户到达时不能安全让路 | M5 |

### 3.3 P1 缺口

| ID | 缺口 | 风险 | 里程碑 |
|---|---|---|---|
| PA-P1-01 | Context fallback 缺端到端实现证据 | 三路语义只有部分链路可验证 | 无内容时的低频主动背景行为不可控 | M2 |
| PA-P1-02 | model score/enrichment 仍为预留 | 固定分数或上游规则难理解复杂语义 | M2 |
| PA-P1-03 | 缺语义近重复抑制 | 不同 ID 的同义消息可能重复推送 | M2、M6 |
| PA-P1-04 | 缺分级 ACK/重新评估窗口 | cited、暂不感兴趣、丢弃的生命周期相同 | M6 |
| DR-P1-01 | 缺 Skill manifest/目录 | Drift 维护项目难扩展和审计 | M4 |
| DR-P1-02 | 用户可见 Drift 结果无统一路径 | 容易绕过 Policy/Delivery 直接发送 | M6 |
| DR-P1-03 | Drift 指标和 Query 目前返回占位值 | 无法判断价值、成本和抢占质量 | M6 |

### 3.4 明确不做

- 不复制 Akashic `proactive_v2` 目录或常驻 tick loop；
- 不让 `PROACTIVE_CONTEXT.md` 成为权威 Policy；
- 不新建与 Task/Attempt 平行的 Drift 状态机；
- 不在 Drift MVP 开放 shell、任意网络写、任意 MCP、插件安装或外部系统修改；
- 不允许 Drift 直接调用 Channel Gateway 或 DeliveryService；
- 不用 LLM 替代确定性 Policy；
- 不用语义去重替代数据库幂等键；
- 不在 PLAN-13 完成前让 Drift 自动确认、删除或重写长期记忆；
- 不默认启用真实主动发送或 Drift，发布时仍采用 opt-in + dry-run。

## 4. 冻结的目标决策

### 4.1 Proactive 责任边界

```text
Model Enricher
  负责：摘要、主题、novelty/relevance/urgency 建议、evidence、confidence
  不负责：发送授权、Quiet Hours、预算、Endpoint、审批

Policy Engine
  负责：allow/deny、阈值、energy、时效、冷却、预算、动作

Delivery
  负责：目标快照、Attempt、重试、平台回执
```

### 4.2 Drift 是 Task mode，不是新领域根

最小 Task 类型：

```text
drift.admit       可选；周期检查并创建 drift.run
drift.run         执行一个已选择的 Skill
drift.project     可选；将用户可见结果投影为 ProactiveCandidate
```

MVP 优先让 Scheduler 直接调用 Admission Service，再创建 `drift.run`；只有在 admission 本身需要独立重试或多阶段时才保留 `drift.admit` Task。

### 4.3 Drift Skill 是声明，不是授权

Skill 由 Markdown 说明和结构化 manifest 组成：

```text
.workspace/drift/skills/<skill-name>/
├── SKILL.md
└── manifest.toml
```

`SKILL.md` 面向模型和人；`manifest.toml` 是机器约束。建议字段：

```toml
name = "proactive-policy-view-audit"
version = "1.0"
description = "检查 Policy 聚合与 Markdown 派生视图是否一致"
handler = "builtin.proactive_policy_view_audit"
risk_level = "low"
allowed_tools = ["filesystem.read:workspace", "query.proactive_policy"]
max_steps = 6
max_runtime_seconds = 30
max_model_calls = 1
max_tool_calls = 8
can_emit_candidate = false
requires_approval = false
checkpoint_schema_version = 1
```

manifest 声明的工具仍需 Capability Policy 逐次授权；声明本身不能放行权限。

### 4.4 强制收尾协议

借鉴 Akashic，但映射到 Cogito Task：

```text
finish_drift(
  status: completed | paused | waiting | skipped,
  summary,
  checkpoint,
  result_ref?,
  candidate_draft?,
  reason_code
)
```

- `completed/skipped` 可以没有后续 Checkpoint；
- `paused/waiting` 必须提供版本化 Checkpoint；
- candidate 只是草稿，由投影服务验证并生成 `ProactiveCandidate(origin=drift)`；
- 不提供 `sent` 状态，因为 Drift 不直接发送；
- 步数或时间耗尽时进入 wrap-up，只允许 Checkpoint/Finish；
- 模型再次失败时 runtime 自动保存 `paused: budget_exhausted`。

### 4.5 全局 Idle 定义

一次 admission 使用同一个事务快照检查：

```text
active_normal_turns == 0
high_priority_task_backlog == 0
ready_delivery_backlog == 0
outbox_critical_age < threshold
recovery_in_progress == false
last_user_activity_age >= idle_after
daily_drift_budget_remaining > 0
model/tool/sqlite resource pressure < threshold
no_active_drift_lease_for_principal
```

“三路没有主动内容”可以提高 Drift 候选分数，但不能单独作为 idle 结论。

### 4.6 用户可见结果统一进入 Candidate

```text
DriftResult(candidate_draft)
  → validate provenance/trust/evidence
  → ProactiveCandidate(stream_type=context, origin=drift)
  → ProactivePolicy
  → send_now | send_later | digest | silent | discard
  → Delivery
```

这样保留 Quiet Hours、预算、同主题冷却、Endpoint 选择和 dry-run。

## 5. 里程碑 M0：基线、契约和测试冻结

### 目标

在修改行为前建立当前快照，修正文档/代码命名歧义，防止把 feedback distribution drift 与 idle Drift 混淆。

### 任务

1. 固化现有 Proactive 测试基线：candidate projection、decision、delivery/digest、scheduler tick、misfire。
2. 为真实发送路径增加当前行为 characterization tests，不立即修改实现。
3. 将 `FeedbackDriftController` 文档和 Dashboard 标签改为 `feedback_distribution_drift`；类名是否迁移由兼容性测试决定，公共 API 暂不改。
4. 新增 ADR：Drift 复用 Task/Attempt/Lease/Checkpoint；禁止独立 `drift.db`。
5. 新增 ADR：Drift 用户可见输出必须先生成 Candidate，不允许直接 Delivery。
6. 冻结 `DriftFinish`、`DriftCheckpoint`、`DriftSkillManifest` Schema v1。

### 主要文件

- `markdown/00_guides/adr/ADR-003_Drift统一任务与投递边界.md`
- `markdown/04_background/04_主动推送与后台空闲处理.md`
- `src/cogito/service/feedback_drift.py`
- `tests/proactive/`
- `tests/architecture/test_feedback_drift.py`

### 验收

- 所有现有相关测试通过；
- ADR 明确第二事实源、直接 push、默认外部副作用均被禁止；
- 评审能逐项对应 `PROACTIVE-IDLE / 9-12`、`TASK-SCHEDULER / 4-10`。

### 预计工作量

2～3 个开发日。

## 6. 里程碑 M1：Proactive 审计与能量闭环

### 目标

修复当前两个 P0：Decision dry-run 记录和真实用户活动 energy。

### 任务

1. 修改 `persist_decision()`，要求调用方显式传入 `dry_run`；删除固定值。
2. 所有调用点传入本次不可变 `config_snapshot.dry_run`。
3. 新增 `PresenceReader` Port，提供 `get_last_user_activity(principal_id)`；实现从权威 Message/Turn 活动读取，不直接依赖 Web session 内存。
4. `TaskHandlerContext` 注入 PresenceReader，`proactive.evaluate` 获取 `last_user_at`。
5. Decision 保存 `energy_value`、`last_user_at`、`energy_model_version`、`config_version_id`。
6. 同一次批量 evaluation 固定一个 `now` 和 activity snapshot，避免逐 Candidate 漂移。
7. 修正 count 查询的日/小时窗口，按 Policy timezone 计算，而非简单 UTC epoch 桶；若决定保持 UTC，必须在规范和 UI 明示。
8. Dashboard 区分 simulated decision、real decision、delivery created、delivery sent。

### Schema/Migration

优先复用现有字段；若缺失则新增 migration（按实际最新序号分配，不硬编码）：

```text
proactive_decisions_v2.last_user_at          INTEGER NULL
proactive_decisions_v2.energy_model_version  TEXT NOT NULL DEFAULT 'v1'
```

`config_version_id` 已存在则不重复新增。

### 主要文件

- `src/cogito/service/proactive_decision.py`
- `src/cogito/service/task_handlers.py`
- `src/cogito/service/energy_model.py`
- `src/cogito/service/api/query_service.py`
- `src/cogito/application.py`
- `src/cogito/store/proactive_repo.py`
- `src/cogito/store/migrations/`
- `tests/proactive/test_decision_engine.py`
- `tests/proactive/test_delivery_digest_loop.py`

### 必测场景

- dry-run 不创建 Delivery，Decision.dry_run=true；
- real mode 创建 Delivery，Decision.dry_run=false；
- 最近活动 1 分钟、1 小时、4 小时、从未活动分别落入正确 energy band；
- 同批 10 个 Candidate 使用同一 activity/energy snapshot；
- 跨午夜 Quiet Hours 与 Policy timezone 正确；
- PresenceReader 失败时 fail-safe：提高发送门槛或进入 digest，不按最低 energy 增强主动性。

### 验收

- PA-P0-01、PA-P0-02 关闭；
- Dashboard 和数据库能从 Decision 追溯当时 energy、活动时间、配置版本；
- 未改变公共 API 行为。

### 预计工作量

3～5 个开发日。

## 7. 里程碑 M2：自适应调度、Context 路与模型增强

### 目标

接通 energy → next schedule，完成三路输入与可选模型语义增强，同时保持确定性授权。

### 任务 A：自适应调度

1. 增加 `ProactiveCadencePolicy`：根据 energy band、backlog、最近发送、失败率计算下一次 interval。
2. Scheduler 持久化下一次 fire，不使用进程内 sleep loop。
3. 为 jitter 使用可注入 RNG/Clock，测试可复现。
4. 配置范围限制：`min_interval <= computed <= max_interval`。
5. misfire 使用 `coalesce`，重启后最多补一次 evaluation，不补发所有错过 tick。
6. Alert 由 Event 立即触发 evaluation，不等待普通 cadence。

### 任务 B：Context fallback

1. 定义 `ContextSignal`，它不是 Event，不要求 event_id，不直接生成发送动作。
2. 仅在本批无 alert/content Candidate 时参与。
3. 加入确定性概率阀、日配额和 reason trace；测试使用 seeded RNG。
4. Context 来源保留 trust label、freshness、source_ref；不可把外部文本当指令。
5. 未命中 fallback 时可请求 Drift admission，但必须再做全局 idle 检查。

### 任务 C：模型增强

1. 新增 `ProactiveEnrichmentService`，输入 Candidate + 有界证据，输出严格 Schema：

```json
{
  "summary": "...",
  "topic": "...",
  "novelty": 0.0,
  "relevance": 0.0,
  "urgency": 0.0,
  "confidence": 0.0,
  "evidence_refs": ["..."],
  "rationale": "...",
  "model_version": "...",
  "prompt_version": "..."
}
```

2. 模型输出只更新候选派生评分，不改变 allow/deny、Quiet Hours、预算或 Delivery。
3. 输出非法、超时或 confidence 过低时回退到规则评分并记录 degradation。
4. 增加语义重复信号：候选与最近主动消息相似时降低 novelty；唯一键仍是硬幂等。
5. 原始外部内容带 `external_untrusted` 边界进入 prompt。

### 主要文件

- `src/cogito/service/scheduler.py`
- `src/cogito/service/energy_model.py`
- `src/cogito/service/proactive_enrichment.py`（新模块有明确单一职责，允许新增）
- `src/cogito/service/proactive_decision.py`
- `src/cogito/service/event_consumers.py`
- `src/cogito/config.py`
- `src/cogito/store/proactive_repo.py`
- `tests/connector/test_scheduler_tick.py`
- `tests/connector/test_scheduler_misfire.py`
- `tests/proactive/test_context_fallback.py`
- `tests/proactive/test_enrichment.py`

### 配置建议

```toml
[proactive.cadence]
min_interval_seconds = 60
max_interval_seconds = 1800
high_energy_interval_seconds = 480
medium_energy_interval_seconds = 240
low_energy_interval_seconds = 60
jitter_ratio = 0.10
misfire_policy = "coalesce"

[proactive.context_fallback]
enabled = false
probability = 0.03
daily_max = 1

[proactive.enrichment]
enabled = false
model_role = "proactive_enricher"
timeout_seconds = 15
minimum_confidence = 0.65
```

### 验收

- energy 同时影响决策和下一次持久 Schedule；
- Alert 可即时触发；普通 evaluation 不形成常驻 loop；
- Context 不绕过配额和 Policy；
- 关闭 enrichment 时行为与 M1 一致；
- 模型失败不会扩大权限或提高发送率。

### 预计工作量

6～8 个开发日。

## 8. 里程碑 M3：Drift 契约、Schema 与准入控制

### 目标

建立 Drift 的领域数据和全局 idle admission，但暂不执行模型或写操作。

### 任务

1. 定义：

```text
DriftSkillManifest
DriftAdmissionSnapshot
DriftRunResult
DriftCheckpointV1
DriftReasonCode
```

2. 复用 `tasks/task_attempts` 保存生命周期；新增 Drift 详情表只保存专属属性，不复制 Task 状态。
3. 新增 `DriftAdmissionService`，在只读事务快照中读取 Turn、Task、Delivery、Outbox、Recovery、Activity、Budget。
4. Admission 输出 `admit | deny` + 结构化 reason list + snapshot time。
5. Scheduler 只在 `admit` 时创建一个幂等 `drift.run` Task。
6. 使用唯一约束保证同 Principal/Profile 同时最多一个 active Drift。
7. 默认 `enabled=false, dry_run=true`；dry-run 只记录本应选择的 Skill，不创建 run Task。
8. Admission 失败不得使用模型。

### Schema 草案

```sql
CREATE TABLE drift_runs (
  drift_run_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
  principal_id TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  skill_version TEXT NOT NULL,
  status TEXT NOT NULL,
  admission_snapshot_json TEXT NOT NULL,
  finish_summary TEXT,
  result_ref TEXT,
  candidate_id TEXT,
  preemption_reason TEXT,
  started_at INTEGER,
  finished_at INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE drift_skill_state (
  principal_id TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  skill_version TEXT NOT NULL,
  last_status TEXT,
  last_run_at INTEGER,
  run_count INTEGER NOT NULL DEFAULT 0,
  checkpoint_ref TEXT,
  cursor_json TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (principal_id, skill_name)
);
```

Task/Attempt 是生命周期权威；`drift_runs.status` 是查询投影，必须由同一事务或 Event Consumer 更新。

### 主要文件

- `src/cogito/domain/drift.py`
- `src/cogito/store/drift_repo.py`
- `src/cogito/service/drift_admission.py`
- `src/cogito/service/scheduler.py`
- `src/cogito/service/task_handlers.py`
- `src/cogito/config.py`
- `src/cogito/store/migrations/`
- `tests/drift/test_admission.py`
- `tests/drift/test_schema.py`
- `tests/drift/test_scheduler.py`

### Admission 必测矩阵

| 条件 | 预期 |
|---|---|
| active normal Turn > 0 | deny `active_turn` |
| 高优先级 Task backlog > 0 | deny `priority_backlog` |
| ready Delivery > 0 | deny `delivery_backlog` |
| Recovery 进行中 | deny `recovery_in_progress` |
| SQLite/model pressure 高 | deny `resource_pressure` |
| 日预算耗尽 | deny `budget_exhausted` |
| 最近用户活动未超阈值 | deny `not_idle_long_enough` |
| 已有 active Drift Lease | deny `drift_already_active` |
| 全部满足 | admit，创建唯一 Task |

### 验收

- DR-P0-02 关闭；
- Admission 完全确定性且不调用模型；
- 并发两个 Scheduler 只创建一个 Drift Task；
- migration upgrade/downgrade/重复执行测试通过。

### 预计工作量

5～7 个开发日。

## 9. 里程碑 M4：Drift Skill 与最小只读执行器

### 目标

实现 Skill 驱动的 Drift MVP，但仅支持内置、低风险、只读或受控派生写 Skill。

### 首批 Skill

1. `proactive-policy-view-audit`：检查 DB Policy 与 Markdown 派生视图一致性，只报告差异。
2. `proactive-candidate-quality-stats`：统计候选质量、重复、决策分布，无用户消息。
3. `embedding-missing-scan`：只生成缺失 Embedding 清单；实际补全由已有普通 Task 执行。
4. `payload-gc-scan`：只生成可回收清单，不删除。

Memory 去重、摘要压缩、索引重建在 PLAN-13 相应契约稳定后接入；MVP 不自动修改 Memory。

### 任务

1. 实现 `DriftSkillCatalog`：扫描内置与 workspace Skill，严格解析 manifest。
2. 同名 Skill 冲突采用确定性优先级；默认内置优先，覆盖需显式配置。
3. Manifest 校验 allowed tools、risk、预算、handler、checkpoint schema。
4. 实现 `DriftSkillSelector`：MVP 先用确定性评分（due、上次状态、失败退避、预期成本、价值）；可选模型只在候选前 3 个中选择。
5. 实现 `drift.run` Handler，复用 Task Worker、Lease 和 ResourceBudget。
6. 工具集合采用 allowlist；MVP 不注册 shell、network write、message send、plugin manage、secret read。
7. 执行器每步写结构化 journal 到 Payload/Trace，不把大内容内联数据库。
8. 实现 `finish_drift` 强制收尾和 budget wrap-up。
9. Skill 没有值得做的事时返回 `skipped/no_value`，不强行执行。

### 选择评分建议

```text
score = due_score
      + expected_value
      + continuation_bonus(paused)
      + staleness_bonus
      - estimated_cost
      - recent_run_penalty
      - recent_failure_penalty
```

评分和权重版本必须进入 admission/selection trace。

### 主要文件

- `src/cogito/domain/drift.py`
- `src/cogito/service/drift_skill_catalog.py`
- `src/cogito/service/drift_selector.py`
- `src/cogito/service/drift_runner.py`
- `src/cogito/service/task_handlers.py`
- `src/cogito/capability/policy.py`
- `.workspace/drift/skills/`（运行时位置，不提交真实用户状态）
- `src/cogito/resources/drift_skills/`（如项目现有资源布局允许；否则放现有 Skill 目录）
- `tests/drift/test_skill_catalog.py`
- `tests/drift/test_selector.py`
- `tests/drift/test_runner.py`

### 验收

- DR-P0-01、DR-P1-01 关闭；
- 首批 4 个 Skill 均可在无模型模式运行；
- 非 allowlist Tool 无法通过 Registry 或 Policy；
- max steps/time/tool/model budget 生效；
- 未调用 finish 时 runtime 自动 paused，并保存可恢复 Checkpoint；
- workspace Skill 无法通过 manifest 声明提升自身权限。

### 预计工作量

7～10 个开发日。

## 10. 里程碑 M5：Checkpoint、抢占、恢复与 Policy

### 目标

让 Drift 在用户 Turn 到来、Lease 丢失、进程崩溃和审批等待时安全停止并恢复。

### 任务

1. 每个 Drift step 前检查：

```text
lease_valid
cancel/preempt_requested
active_normal_turns
priority_backlog
budget_remaining
```

2. 新 Turn 入站提交后，发出/更新 Drift preemption signal；不要求同步等待 Drift 停止。
3. Drift 在安全点写 `DriftCheckpointV1`，更新 TaskAttempt.checkpoint_ref，释放 Lease。
4. 不可中断的本地原子读操作可完成；不得开始新的写或外部副作用。
5. Lease 续租失败立即停止领取新 step。
6. 恢复前校验 config version、capability snapshot version、Skill version、checkpoint schema。
7. 不兼容 Checkpoint 进入 `needs_review` 或从安全起点 retry，不静默继续。
8. 中风险本地写操作必须通过 Policy；高风险操作转换为 Command/Approval/普通 Task。
9. Budget 消耗保存于 Task/Attempt，恢复后累计而非重置。
10. 加入失败退避和 circuit breaker；连续失败 Skill 暂停自动选择。

### Checkpoint V1

```json
{
  "schema_version": 1,
  "drift_run_id": "...",
  "task_id": "...",
  "attempt_id": "...",
  "skill_name": "...",
  "skill_version": "...",
  "step_index": 3,
  "cursor": {},
  "completed_actions": [],
  "pending_action": null,
  "budget_used": {},
  "config_version_id": "...",
  "capability_snapshot_version": "...",
  "created_at": "..."
}
```

不得在 Checkpoint 中保存 Secret、原始大 Payload、未脱敏 Tool 输出或可变 Provider 对象。

### 主要文件

- `src/cogito/service/drift_runner.py`
- `src/cogito/service/drift_preemption.py`
- `src/cogito/service/recovery_decision.py`
- `src/cogito/service/task_worker.py`
- `src/cogito/service/inbound_service.py`
- `src/cogito/store/task_repo.py`
- `src/cogito/runtime/loop.py`
- `tests/drift/test_preemption.py`
- `tests/drift/test_checkpoint.py`
- `tests/drift/test_recovery.py`
- `tests/drift/test_policy.py`

### 故障注入场景

- step 执行前到达新 Turn；
- step 执行中 Lease 续租失败；
- Checkpoint 写入后进程退出；
- Task 状态提交前进程退出；
- Skill 升级导致 Checkpoint version 不兼容；
- Policy 从 allow 改为 deny 后恢复；
- Budget 只剩一次 Tool call；
- SQLite busy/readonly；
- 模型返回非 finish tool 或非法 finish payload。

### 验收

- DR-P0-03 关闭；
- 新 Turn 到达后 P95 在一个安全 step 内停止 Drift，目标时间 ≤ 2 秒或当前原子操作上限；
- 崩溃恢复不重复已确认副作用；
- Lease 丢失后零新副作用；
- 恢复后预算不重置。

### 预计工作量

7～10 个开发日。

## 11. 里程碑 M6：用户可见结果、Dashboard 与反馈

### 目标

让有价值的 Drift 结果安全进入主动通知，并让用户理解“为什么运行、做了什么、为什么发送或没发送”。

### 任务 A：结果投影

1. 定义 `DriftCandidateDraft`：topic、summary、evidence_refs、trust labels、urgency、expiry、suggested target。
2. 投影服务校验来源和 Principal，生成幂等 `ProactiveCandidate(origin=drift)`。
3. Candidate 继续走原有 Policy、Quiet Hours、budget、Digest 和 Delivery。
4. 同一 DriftRun 最多生成一个用户可见 Candidate；可生成多个内部 result item。
5. dry-run 保存 preview，不创建真实 Candidate/Delivery。

### 任务 B：反馈和分级 ACK

1. 统一反馈：accepted、dismissed、not_relevant、too_frequent、duplicate、wrong_time。
2. 将反馈映射为策略分析信号，不直接让模型改 Policy。
3. 为 Candidate/Event 增加重新评估窗口：
   - cited/sent：长 ACK；
   - interesting but not sent：短 ACK，可再次评估；
   - duplicate/discarded：更长抑制；
   - alert：一次性消费或由来源定义。
4. 增加语义近重复 trace 和人工反馈回放集。

### 任务 C：Dashboard/Query

Dashboard 最少显示：

```text
Proactive
  Candidate → Enrichment → Decision → Delivery
  energy / last_user_at / policy version / dry-run
  rule trace / model evidence / duplicate signal

Drift
  Admission result / deny reasons
  selected Skill / score / version
  Task / Attempt / Lease / Checkpoint
  steps / budget / preemption
  internal result / generated candidate
```

所有控制动作：enable/disable、run once、pause Skill、reset circuit、approve operation，必须走 Command API 和 Audit。

### 主要文件

- `src/cogito/service/drift_projection.py`
- `src/cogito/service/proactive_feedback.py`
- `src/cogito/service/api/query_service.py`
- `src/cogito/service/api/command_service.py`
- `src/cogito/interaction_web/query.py`
- `web/`
- `tests/drift/test_projection.py`
- `tests/proactive/test_feedback.py`
- `tests/interaction_web/`

### 验收

- Drift 无法直接调用 DeliveryService；
- Candidate 生成后可完整追溯到 DriftRun/TaskAttempt/evidence；
- Quiet Hours、budget、cooldown 对 Drift 来源同样有效；
- Dashboard 不再返回 `drift_preemption_reason=None` 占位；
- 所有控制动作产生 Audit。

### 预计工作量

6～8 个开发日。

## 12. 里程碑 M7：回放、评测、性能与发布门禁

### 目标

证明系统“有用而不打扰、空闲而不争抢、恢复而不重复、智能而不越权”。

### 离线回放集

至少包含：

- alert：必须及时但受明确 deny/Quiet Hours 策略；
- content：相关、无关、过期、重复、跨来源同义；
- context：有价值 fallback、无价值闲聊、敏感时刻；
- Drift：有工作、无工作、连续 paused、长期 waiting、失败退避；
- 新 Turn 抢占；
- Delivery/Outbox backlog；
- 重启/misfire/Lease 丢失；
- Prompt injection 内容；
- 模型非法 JSON、幻觉 evidence、工具越权。

### 核心指标

```text
Proactive
send_rate
accept_rate
dismiss_rate
duplicate_rate
wrong_time_rate
alert_latency_p95
decision_explainability_coverage
dry_run_real_state_mismatch = 0

Drift
admission_rate
no_value_rate
completion_rate
paused_recovery_rate
preemption_latency_p95
duplicate_side_effect_count = 0
unauthorized_tool_execution_count = 0
model_cost_per_useful_result
user_visible_candidate_accept_rate
```

### 发布门禁

1. 全量测试通过，相关模块分支覆盖率 ≥ 85%。
2. 100 次崩溃/恢复故障注入无重复外部副作用。
3. 100 次并发 admission 只产生一个 active Drift。
4. 新 Turn 抢占 P95 达标。
5. 所有 unauthorized Tool 测试均在执行前被拒绝。
6. dry-run 连续运行 7 天，无预算越界、无真实 Delivery。
7. 小流量真实 proactive 运行 7 天后，才允许 Drift candidate 真实发送。
8. Drift MVP 首次发布只启用 L0/L1 内置 Skill；workspace Skill 默认 disabled。
9. 备份恢复后 Task/Drift/Decision/Delivery 因果链可查询。

### 分阶段开关

```text
Stage 0  proactive dry-run + drift disabled
Stage 1  proactive real for allowlisted topics + drift admission dry-run
Stage 2  drift internal read-only skills real, no candidate emission
Stage 3  one allowlisted drift skill may emit candidate, proactive policy still governs
Stage 4  workspace skills opt-in, per-skill review and budget
```

### 主要文件

- `tests/replay/proactive/`
- `tests/replay/drift/`
- `tests/integration/test_proactive_drift_e2e.py`
- `tests/integration/test_drift_recovery.py`
- `tests/security/test_drift_tool_policy.py`
- `docs/operations/proactive.md`
- `docs/operations/drift.md`
- `config.example.toml`

### 验收

- 所有发布门禁有自动化证据；
- 本地运行手册包含启用、dry-run、查看、暂停、恢复、回滚；
- Stage 0 默认值不产生用户可见行为。

### 预计工作量

5～7 个开发日。

## 13. 实施依赖与并行策略

### 串行依赖

```text
M0 → M1 → M2
M0 → M3 → M4 → M5 → M6 → M7
M2 ────────────────────→ M6
```

### 可并行工作

- M1 的 audit/energy 与 M3 的 Drift domain/schema 可并行，但 migration 编号需协调。
- M2 的 enrichment 与 M4 的内置只读 Skill 可并行。
- M5 的 preemption 和 M6 的 Dashboard Query 可并行，前提是 Query 先使用稳定 DTO。
- PLAN-13 可与 M0～M5 并行；Memory 写入型 Drift Skill 等 PLAN-13 M1～M3 完成后再接入。

### 建议迭代节奏

单人全职估算 41～58 个开发日，建议拆为 6 个发布迭代：

| 迭代 | 内容 | 可交付结果 |
|---|---|---|
| R1 | M0 + M1 | Proactive 审计与真实 energy 正确 |
| R2 | M2 | 自适应调度、Context、可选模型增强 |
| R3 | M3 | Drift Schema 与 admission dry-run |
| R4 | M4 | 只读内置 Drift Skill 可运行 |
| R5 | M5 + M6 | 可抢占恢复、Candidate 统一投影、Dashboard |
| R6 | M7 | 回放、故障注入、灰度发布 |

若只做最小高价值版本，完成 M0～M5 即可，约 30～43 个开发日；M6～M7 不应无限延期，但可在内部验证后单独发布。

## 14. 测试金字塔

### 单元测试

- energy/cadence/Quiet Hours/timezone；
- admission rules/reason codes；
- manifest validation/selector scoring；
- finish/checkpoint schema；
- Policy allow/deny/approval；
- semantic duplicate signal fallback。

### Repository/Migration 测试

- upgrade/reapply/rollback-compatible；
- unique active Drift；
- Task 与 Drift projection 一致；
- Decision dry-run 与 Delivery 事实一致；
- checkpoint/cursor roundtrip。

### 集成测试

- Event → Candidate → Enrichment → Decision → Delivery；
- no content → admission → drift.run → internal result；
- Drift result → Candidate → Digest/Delivery；
- Turn arrival → preemption → checkpoint → resume；
- crash → lease expiry → recovery。

### 安全测试

- Skill prompt injection；
- manifest 请求未授权 Tool；
- path traversal/symlink escape；
- secret read；
- shell/network/message send；
- MCP 动态挂载；
- forged candidate evidence；
- resume 后 Policy 变化。

### 回放与评测

- 固定 Clock/RNG/Model response；
- 保存 Candidate、Policy、energy、model output、Decision trace；
- 比较版本前后发送率、接受率、误打扰和成本；
- 任何阈值变更必须附回放差异报告。

## 15. 配置总览

建议最终配置结构：

```toml
[proactive]
enabled = false
dry_run = true
default_principal_id = "owner"

[proactive.cadence]
enabled = true
min_interval_seconds = 60
max_interval_seconds = 1800
misfire_policy = "coalesce"

[proactive.enrichment]
enabled = false
model_role = "proactive_enricher"
minimum_confidence = 0.65

[proactive.context_fallback]
enabled = false
probability = 0.03
daily_max = 1

[drift]
enabled = false
dry_run = true
idle_after_minutes = 30
max_runs_per_day = 3
max_concurrent = 1
max_runtime_seconds = 60
max_steps = 8
allow_workspace_skills = false
allow_candidate_emission = false

[drift.preemption]
check_interval_seconds = 1
turn_priority_threshold = 50
high_priority_backlog_threshold = 1

[drift.skills.proactive-policy-view-audit]
enabled = true

[drift.skills.proactive-candidate-quality-stats]
enabled = true
```

配置解析必须严格拒绝未知字段；默认值不得启用真实发送或外部副作用。

## 16. 回滚策略

### Proactive

- `proactive.enrichment.enabled=false` 回退确定性评分；
- cadence 异常时切回固定 interval Schedule；
- context fallback 独立关闭；
- real mode 异常时立即切 `dry_run=true`，已有 Delivery 按 Command 决定暂停或继续，不能静默删除。

### Drift

- 全局 `drift.enabled=false` 停止创建新 Task；
- Worker 不再领取新的 drift.run；已有 Attempt 在安全点 Checkpoint；
- 单 Skill circuit/pause 不影响其他后台任务；
- migration 采用 expand-first，旧版本忽略新表；
- 不删除 Drift 历史、Checkpoint、Receipt 或 Audit；
- candidate emission 可独立关闭，内部只读维护仍可运行。

## 17. Definition of Done

本计划仅在以下条件全部满足后完成：

- [x] Decision dry-run 与真实发送状态一致；
- [x] energy 使用真实用户活动并驱动决策与持久调度；
- [x] Alert、content、context 三路有端到端测试；
- [ ] 模型增强只提供信号，不能绕过 Policy；（M2 Task C 显式未做）
- [x] Drift admission 是全局、确定性、事务快照；
- [x] Drift 使用 Task/Attempt/Lease/Checkpoint，无第二事实源；
- [x] Drift 默认 Tool 集只读且无直接发送能力；
- [x] 新 Turn、Lease 丢失和预算耗尽均能安全抢占；
- [x] paused/waiting 可从兼容 Checkpoint 恢复；
- [x] 用户可见 Drift 结果先生成 Candidate，再走 Policy/Delivery；
- [x] Dashboard 可解释 admission、selection、decision、preemption、delivery；
- [x] dry-run、回放、故障注入和安全门禁通过；
- [x] 配置、Migration、运维手册和示例同步更新；
- [x] 默认安装不产生主动消息或 Drift 外部副作用。

## 18. 第一批可直接执行的 Issue

### Issue 1：修复 Decision dry-run 事实

- 改 `persist_decision(..., dry_run: bool)`；
- 更新调用点；
- 增加 dry/real 两条集成测试；
- 更新 Dashboard 指标断言。

### Issue 2：接入 PresenceReader

- 定义 Port；
- 从 Message/Turn 权威时间读取；
- 注入 TaskHandlerContext；
- 保存 last_user_at/energy/version；
- 增加读取失败的 fail-safe 测试。

### Issue 3：实现 ProactiveCadencePolicy

- 固定 Clock/RNG；
- energy band → interval；
- 上下限与 jitter；
- Schedule coalesce；
- Alert immediate trigger。

### Issue 4：冻结 Drift ADR 与 DTO

- ADR-003；
- `DriftAdmissionSnapshot`、`DriftFinish`、`DriftCheckpointV1`；
- 明确禁止第二状态机和直接发送。

### Issue 5：实现 DriftAdmissionService

- 读取 Turn/Task/Delivery/Outbox/Recovery/Budget；
- reason codes；
- 并发唯一性；
- dry-run Query。

### Issue 6：实现首个只读 Skill

- `proactive-policy-view-audit` manifest + handler；
- 无模型执行；
- finish/checkpoint；
- Tool allowlist 安全测试。

执行完 Issue 1～3，Cogito 的 Proactive 就从“功能可用”提升到“信号和审计闭环正确”；执行完 Issue 4～6，则获得一个真正安全、可观察、可恢复的 Drift 最小纵切面。
