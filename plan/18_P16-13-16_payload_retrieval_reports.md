---
plan_id: "PLAN-16-13"
title: "PLAN-16 收尾：Payload Store / Unified Retrieval / 报告 / 状态"
version: "1.0"
status: "completed"
created_at: "2026-07-12"
owner: "Cogito"
scope: "修复 Knowledge Payload Store 运行时错误；完成真正全来源 Unified Retrieval；生成 baseline/memU 报告；同步 PLAN-14/16/manifest 完成状态"
depends_on:
  - "PLAN-16 M0-M7"
  - "RETRIEVAL-CONTEXT"
  - "DOMAIN-CONTRACTS"
  - "MEMORY-LIFECYCLE"
---

# PLAN-16 收尾计划（P16-13 → P16-16）

## 背景

PLAN-16 M0-M7 非测试生产缺口已通过 374 测试。但深度审计发现 4 类残留问题：
1. Knowledge Payload Store 存在 3 个运行时 bug（classmethod 自引用、签名不匹配、handler 不传参），导致 `knowledge.enabled=true` 时 Application build 阶段直接 NameError；
2. Unified Retrieval 仍有独立选择逻辑，未做到"所有来源共享一个候选和预算决策"；
3. baseline/memU 报告未生成；
4. PLAN-14/16/manifest 过早标记 completed，清单未按真实结果勾选。

推荐提交顺序：**P16-13（PayloadStore）→ P16-14（Unified Retrieval）→ P16-15（报告）→ P16-16（状态同步）**。

---

## P16-13：修复 Knowledge Payload Store 运行时路径

### 目标

`knowledge.enabled=true` 时 RuntimeApplication 可正常构建；大正文不再内联进 Task payload；payload 丢失时 Task 明确失败。

### 1.1 修复 classmethod 中错误的 self 引用

`application.py:399` 在 `RuntimeApplication.build(cls, config)` 内：

```python
set_payload_store_factory(self._make_payload_store_factory())
```

没有 self。直接构造工厂并注入：

```python
def _make_payload_store(payload_conn=None):
    return PayloadStore(config.resolve_payload_dir(), payload_conn or conn)

knowledge_service = KnowledgeService(
    conn, embedder=knowledge_embedder,
    payload_store_factory=_make_payload_store,
)

def _make_knowledge_service(knowledge_conn):
    return KnowledgeService(
        knowledge_conn, embedder=knowledge_embedder,
        payload_store_factory=lambda payload_conn=None: PayloadStore(
            config.resolve_payload_dir(), payload_conn or knowledge_conn,
        ),
    )
```

**效果**：不依赖 multimodal 开关；不引用未创建的 app/self；共享 knowledge_service 也能解析 payload。

### 1.2 移除全局 PayloadStore 工厂

`knowledge/sync.py` 的模块级 `_shared_payload_store_factory` / `set_payload_store_factory()` 会产生全局状态与并发污染。改为给现有函数增加显式 `make_payload_store=None` 参数：

- `enqueue_knowledge_sync_source(conn, ..., make_payload_store=None) -> str | None`: 调用 `store = make_payload_store(conn)`；
- 接线位置：Connector Handler 传 `ctx.payload_store_factory`；MCP Connector Handler 传 `ctx.payload_store_factory`；API Command 基于 `deps.config` 和 `deps.conn` 创建；Knowledge Task Handler 传 `ctx.payload_store_factory`。

### 1.3 sync_resource 增加 payload_ref 参数

函数体使用了 `payload_ref` 但签名没定义 → NameError。修改为：

```python
def sync_resource(
    conn: sqlite3.Connection,
    *,
    stable_source_id: str,
    raw_text: str = "",
    payload_ref: str = "",
    source_kind: str = "explicit_local_file",
    content_hash: str = "",
    principal_id: str = "",
    trust_label: str = "unverified",
    make_payload_store=None,
) -> str:
```

解析正文逻辑：

```python
effective_raw_text = raw_text
if not effective_raw_text and payload_ref:
    if make_payload_store is None:
        raise RuntimeError("payload store is not configured")
    store = make_payload_store(conn)
    raw = store.get(payload_ref)
    if raw is None:
        raise RuntimeError(f"knowledge payload not found: {payload_ref}")
    effective_raw_text = raw.decode("utf-8", errors="replace")
if not effective_raw_text:
    raise ValueError("knowledge source contains no content")
```

**关键**：payload 丢失 → 抛异常、Task 失败重试；不把 payload 丢失当空正文。

### 1.4 Task Handler 必须传 payload_ref

`task_handlers.py:317` `_handle_knowledge_sync_source` 调用 sync_resource 时增加：

```python
payload_ref=str(data.get("payload_ref", "")),
make_payload_store=ctx.payload_store_factory,
```

### 1.5 明确 inline/payload 阈值

规则：
- 正文 <= 4096 bytes → 可以内联 raw_text
- 正文 > 4096 bytes → 必须写 PayloadStore，只保存 payload_ref
- PayloadStore 写入失败 → Command/Task 失败并重试，**不降级为截断正文**

**理由**：当前"失败后截断到 50000 字符并内联"会造成 Task 表无限增长、内容截断、source hash 与实际摄取正文不一致。

### 1.6 Payload 与 Task 同事务

写入顺序：
1. `PayloadStore.put`
2. `payload_objects` 行
3. Task 行
4. commit

任何 DB 写入失败都 rollback。外部 payload 文件若已写入但事务失败，由现有 orphan reconcile/GC 清理。

### 完成条件

- [x] knowledge.enabled=true 时 RuntimeApplication 可构建
- [x] multimodal 关闭不影响 Knowledge Payload
- [x] 大正文 Task payload 中 raw_text=""
- [x] payload_ref 指向真实 payload object
- [x] Handler 能读取正文并完成 ingest
- [x] payload 丢失时 Task 明确失败，不生成空 Resource

### 建议提交信息

```
fix(P16-13): complete knowledge payload store transaction path
```

---

## P16-14：完成真正的 Unified Retrieval

### 目标

所有检索来源先产生 RetrievalCandidate；只调用一次统一选择函数；BudgetAllocation.used 有实际值；protected candidate 不被预算删除；余额可跨来源共享；Snapshot 可解释。

### 2.1 第一阶段：只召回候选，不生成 ContextItem

各 Retriever 返回 `list[RetrievalCandidate]`。候选集合：

```python
candidate_groups = {
    "recent_message": message_candidates,
    "session_summary": summary_candidates,
    "memory": memory_candidates,
    "knowledge_segment": knowledge_candidates,
    "task_state": task_candidates,
}
```

`_inject_memories()` / `_inject_knowledge()` 内不再直接生成 ContextItem。

### 2.2 明确 protected candidates

使用 ID 集合，不使用数组下标（下标在排序/过滤/合并后失效）：

```python
protected_ids = {
    input_message_id,
    system_policy_id,
    *active_goal_ids,
    *constraint_ids,
    *recent_k_message_ids,
}
```

System Policy 继续作为非检索固定项；当前输入应作为 protected recent-message candidate。

### 2.3 硬过滤

统一选择前执行：
- principal 不一致 → unauthorized_principal
- scope 不可见 → scope_mismatch
- stale/deleted/expired → inactive
- superseded → superseded
- trust 不满足 → trust_policy
- canonical/source 重复 → duplicate

每个排除候选保留完整 score 和 source refs。

### 2.4 按来源归一化评分

继续使用现有 `normalize_scores()`，在选择前对所有 source group 完成。然后合并：

```python
all_candidates = [
    *message_candidates,
    *summary_candidates,
    *memory_candidates,
    *knowledge_candidates,
    *task_candidates,
]
```

### 2.5 扩展预算分配函数

现有 `budget.py:63` `allocate_budget()` 只生成 quota。增加选择函数：

```python
@dataclass
class BudgetSelection:
    selected: list[RetrievalCandidate]
    excluded: list[RetrievalCandidate]
    allocations: dict[str, BudgetAllocation]

def select_candidates(
    candidates,
    allocations,
    *,
    protected_ids,
) -> BudgetSelection:
```

执行规则：
- protected 候选先选
- protected 消耗对应来源预算，但不会被拒绝
- 普通候选按 final_score 排序
- 优先使用本来源 quota
- 来源未使用余额进入共享池
- 无法容纳的候选标记 token_budget
- 更新每个 allocation 的 used 和排除原因

这样 `allocate_budget()` 返回值不再只是赋值后闲置。

### 2.6 一次性转换 ContextItem

选择完成后统一转换：

```python
items = fixed_system_items + [
    candidate_to_context_item(candidate)
    for candidate in selection.selected
]
```

按语义顺序排序：
- system policy
- memory/goal/constraint
- knowledge
- summary
- task state
- historical messages
- current input

Message 内部仍按 receive_sequence 排序。

### 2.7 删除可变实例状态

当前使用 `self._budget_allocation` / `self.all_excluded` 属于单次 build 临时状态，不应保存在共享 ContextBuilder 上。改为局部变量：

```python
allocations = allocate_budget(...)
selection = select_candidates(...)
all_excluded = selection.excluded
```

Snapshot 直接使用：

```python
excluded=tuple(candidate_to_provenance(c) for c in all_excluded)
```

避免上一轮 Turn 的排除项污染下一轮。

### 2.8 Emergency clip 只作为安全兜底

统一预算完成后，正常情况下不应再超限。保留 `_clip_to_budget()`，只作为异常保护，并记录 `exclusion_reason = emergency_budget_guard`。

### 完成条件

- [x] 所有检索来源先产生 RetrievalCandidate
- [x] 只调用一次统一选择函数
- [x] BudgetAllocation.used 有实际值
- [x] protected candidate 不被预算删除
- [x] 余额可以跨来源共享
- [x] Snapshot 保存每条未选候选及原因
- [x] ContextBuilder 不在实例字段保存单次 Turn 状态

### 建议提交信息

```
refactor(P16-14): complete global retrieval candidate selection
```

---

## P16-15：生成两个报告

### 3.1 plan13-memory-baseline.json

路径 `.workspace/reports/plan13-memory-baseline.json`。结构：

```json
{
  "schema_version": "1",
  "generated_at": "...",
  "config_version": "...",
  "memory": {
    "total": 0, "confirmed": 0, "candidate": 0, "expired": 0,
    "source_coverage_ratio": 0.0,
    "signals_by_type": {},
    "weight_algorithm_versions": {}
  },
  "extraction": {
    "watermarks": 0, "max_lag": 0, "queued_tasks": 0, "failed_tasks": 0
  },
  "knowledge": {
    "resources": 0, "documents": 0, "segments": 0,
    "embedded_segments": 0, "payload_segments": 0
  },
  "context": {
    "snapshots": 0, "tokens_by_source": {}, "exclusions_by_reason": {}
  }
}
```

**不包含**：Memory value、Message 正文、本地真实路径、API Key、Payload 内容。

### 3.2 memU-PoC-decision.md

路径 `.workspace/reports/memU-PoC-decision.md`。章节：

- Decision: Reject production integration
- 评估范围
- Cogito 当前 Memory/Knowledge 能力
- memU 可借鉴能力
- 不直接集成原因
- 数据主权与依赖风险
- 未执行真实 memU 环境评测的证据限制
- 重新评估条件
- 最终决策与日期

明确：`reference/memU` 不进入生产依赖；reference 目录继续被 Git 忽略；允许借鉴设计，不复制运行时依赖。

### 完成条件

- [x] `.workspace/reports/plan13-memory-baseline.json` 存在且 schema 正确
- [x] `.workspace/reports/memU-PoC-decision.md` 存在且 Decision=Reject
- [x] 报告只包含统计信息，不泄露敏感内容

### 建议提交信息

```
docs(P16-15): add memory baseline and memU decision report
```

---

## P16-16：修正文档状态

### 4.1 代码和报告完成前保持

`status: "in_progress"`。

### 4.2 完成后

- [ ] 把 PLAN-16 最后的 16 项 `[ ]` 按真实结果改为 `[x]`
- [ ] YAML 状态只写 `status: "completed"`，不带说明
- [ ] PLAN-14 同步 completed
- [ ] manifest 的 PLAN-14/PLAN-16 同步
- [ ] 更新计划开头"当前完成度 68%"等过时文字

### 完成条件

- [x] PLAN-14/16 与 manifest 状态和真实实现一致
- [x] 清单勾选与实际完成结果匹配
- [x] 无虚假 completed

### 建议提交信息

```
docs(P16-16): reconcile PLAN-14 PLAN-16 completion status
```

---

## 推荐执行顺序

1. **P16-13** PayloadStore（最高优先级：当前 knowledge.enabled=true 可能在 Application build 阶段因未定义的 self 直接失败）
2. **P16-14** Unified Retrieval
3. **P16-15** 报告
4. **P16-16** 状态同步

每个提交独立推送，便于回滚。

## 总体完成定义

- [x] Knowledge enabled 时 Worker 正常启动
- [x] 所有上下文来源共享候选池和单一预算选择
- [x] 报告就位
- [x] 文档状态与实现一致
