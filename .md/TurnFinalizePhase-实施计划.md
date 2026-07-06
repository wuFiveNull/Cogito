# TurnFinalizePhase 实施计划

> 实施日期：2026-06-24
> 状态：已完成

---

## 1. 目标

将 `TurnFinalizePhase` 从最小空壳改造为 **无外部 I/O、确定性、可重复验证的结果封装阶段**。

## 2. 核心问题

旧代码存在 3 个关键缺陷：

1. **状态不一致**：`TurnResult.status` 用 `ctx.status`（RUNNING），而 Kernel 在 Phase 完成后才设 `ctx.status=COMPLETED` → Result 状态错误
2. **静默容错**：`turn_id=None` 变 `""`，`output_text=None` 变 `""` → 掩盖上游 bug
3. **安全泄漏**：`dict(ctx.request.metadata)` 全量复制 → 内部对象、Prompt、Adapter 全部暴露

## 3. 改动步骤

### Step 1: 新增 `InvalidTurnStateError`

**文件**：`cogito/agent/runtime/errors.py`

新增异常类，继承 `RuntimeAgentError`，code=`"INVALID_TURN_STATE"`，`retryable=False`。

用于 Finalize 校验失败时的稳定错误。

### Step 2: 重写 `TurnFinalizePhase`

**文件**：`cogito/agent/runtime/phases/turn_finalize.py`

重写后结构：

```
TurnFinalizePhase
├── _require_turn_id()          — 校验 turn_id 非 None 且非空
├── _require_output_text()      — 仅检查 None，空字符串合法
├── _resolve_final_status()     — RUNNING → COMPLETED；COMPLETED → COMPLETED（幂等）；其他拒绝
├── _build_result_metadata()    — 白名单：finish_reason / response_format / output_language
├── _store_result()             — 无结果写入、相同结果 no-op、冲突抛错
└── execute()                   — 顺序调用以上，保持 async 统一 Phase 协议
```

不应放入该阶段的逻辑：
- 模型调用 / 工具执行 / 检索
- 数据库提交 / MessageBus 发布
- 事件生命周期发布
- 异常吞掉或降级为伪成功
- 锁释放、连接关闭等清理（归 Cleanup）

### Step 3: 修正 `RuntimeKernel`

**文件**：`cogito/agent/runtime/kernel.py`

- **去掉** `ctx.status = TurnStatus.COMPLETED`（此职责移至 FinalizePhase）
- **增加** 防御性校验：`ctx.result.status is not ctx.status` 时抛 `InvalidTurnStateError`
- 保持 `TURN_STARTED → PHASE_STARTED/COMPLETED → TURN_COMPLETED` 事件顺序不变

### Step 4: 新增单元测试

**文件**：`tests/agent/runtime/phases/test_turn_finalize.py`

16 个测试用例覆盖：

| 类别 | 用例 |
|---|---|
| 正常构建 | 全部字段映射正确 |
| turn_id 校验 | None 和空字符串均拒绝 |
| output_text 校验 | None 拒绝，空字符串保留 |
| 快照隔离 | Finalize 后修改 ctx 不污染 result |
| Metadata 白名单 | 只暴露允许的 key；无匹配时返回空 dict |
| 幂等性 | 相同 context 重复执行 no-op |
| 冲突检测 | 修改 context 后重复执行抛错 |
| 状态收敛 | RUNNING → COMPLETED；已 COMPLETED 接受 |
| 非法状态 | CREATED/FAILED/CANCELLED 拒绝 |
| error 检测 | ctx.error 非 None 时拒绝 |

### Step 5: 补充集成测试

**文件**：`tests/agent/runtime/test_kernel.py`

- 真实 `TurnFinalizePhase` 与 Kernel 集成
- Finalize 失败时 `PHASE_FAILED` + `TURN_FAILED` 事件顺序
- Cancel + Finalize 时 Cleanup 仍执行

## 4. 架构边界

```
TurnFinalizePhase
    输入：TurnContext
    依赖：无外部 Port
    输出：
        ctx.status = COMPLETED
        ctx.result = immutable TurnResult

RuntimeKernel
    校验 ctx.result.status == ctx.status
    发送 TURN_COMPLETED / TURN_FAILED
    返回结果或映射错误

RuntimeCleanup
    finally 中执行
    关闭 Trace
    记录 completed_at

AgentMessageWorker
    TurnResult → MessageEnvelope
    发布到 reply_to / agent.output
```

## 5. 影响范围

| 文件 | 改动类型 |
|---|---|
| `cogito/agent/runtime/errors.py` | 新增 +6 行 |
| `cogito/agent/runtime/phases/turn_finalize.py` | 重写 |
| `cogito/agent/runtime/kernel.py` | 改 ~3 行，去掉冗余状态设置 |
| `cogito/agent/runtime/__init__.py` | 导出 `InvalidTurnStateError` |
| `cogito/agent/__init__.py` | 导出 `InvalidTurnStateError` |
| `tests/agent/runtime/phases/test_turn_finalize.py` | 新增，16 个用例 |
| `tests/agent/runtime/test_kernel.py` | 新增 3 个集成测试 + fix helper |

不需改动的文件：`context.py`、`models.py`、`phase.py`、`cleanup.py`、`bootstrap/runtime_factory.py`

## 6. 验收标准

- [x] `TurnFinalizePhase` 不导入 MessageBus、Channel、Repository 或 Model Adapter
- [x] `TurnFinalizePhase` 不执行外部 I/O
- [x] 缺少 `turn_id` 时明确失败
- [x] 缺少 `output_text` 时明确失败
- [x] 不把空字符串自动视为错误
- [x] 正常路径将状态收敛为 `COMPLETED`
- [x] `TurnResult.status` 与 `TurnContext.status` 一致
- [x] Tool Records 被转换为不可变 tuple
- [x] Metadata 只按白名单公开
- [x] 重复执行同一 Finalize 是幂等的
- [x] 冲突结果不会被静默覆盖
- [x] Finalize 失败时 Kernel 发出 `PHASE_FAILED` 和 `TURN_FAILED`
- [x] Finalize 失败时 Cleanup 仍执行
- [x] Kernel 不包含针对 `turn_finalize` 名称的业务分支
- [x] 全部 103 个测试通过
