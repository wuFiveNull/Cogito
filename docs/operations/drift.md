# Drift（空闲后台维护）操作手册

> 遵循 PLAN-14 §4 Drift 是 Task mode（不是独立 Agent Loop），复用 tasks/task_attempts 生命周期。

## 快速启用（Stage 0 → Stage 2）

Drift 默认关闭 + dry-run。启用路径（建议按 Stage 渐进）：

### Stage 0：全关（默认）

```toml
[drift]
enabled = false
dry_run = true
```

### Stage 1：Drift admission dry-run

```toml
[drift]
enabled = true                # 开启 admission tick
dry_run = true                # 仅记录本应选择的 Skill，不创建 run Task
idle_after_minutes = 30
max_runs_per_day = 3
```

观察日志 `[drift-admission]` / `[dry_run] drift admission would select skill`。

### Stage 2：只读内置 Skill 实际运行（不发送 Candidate）

```toml
[drift]
enabled = true
dry_run = false               # 创建真实 drift.run Task
allow_workspace_skills = false
allow_candidate_emission = false
max_runtime_seconds = 60
max_steps = 8
```

重启 cogito。

## 分阶段开关（Stage 0 → Stage 4）

| Stage | 配置 | 效果 |
|---|---|---|
| 0 | `enabled=false` | 全关 |
| 1 | `enabled=true, dry_run=true` | admission/evaluate 仅记录 |
| 2 | `dry_run=false` | 只读内置 Skill 运行，不发 Candidate |
| 3 | `allow_candidate_projection=true` | DriftResult 可投影为 ProactiveCandidate (origin=drift) |
| 3b | `allow_candidate_emission=true` | 真正送评估 (Candidate status=evaluating，走 Decision Engine) |
| 4 | `allow_workspace_skills=true` | workspace Skill opt-in（需逐项 review + budget） |

## Drift 数据流

```
[Scheduler.tick_drift_admit()]
  → DriftAdmissionService.admit()  (8 项 idle 矩阵，确定性)
    ├ deny  → 记原因 (active_turn/priority_backlog/delivery_backlog/
    │          outbox_critical/recovery/budget/not_idle/drift_already_active)
    └ admit → 创建幂等 drift.run Task (origin=drift-admission)
              写 drift_runs (status=admitted)

[TaskWorker 领取 drift.run]
  → drift_runner.handle_drift_run()
    ├ 检测 paused+result_ref → resume 路径
    │   → validate_checkpoint_for_resume (config/skill/checkpoint schema)
    │     ├ 兼容 → 从 step_index+1 续跑 (budget 累计)
    │     └ 不兼容 → needs_review
    └ 从 step 0 启动多步循环
       每步前 should_preempt_step()
        ├ lease 无效 → 暂停 (lease_lost)
        ├ 抢占信号 → 暂停 (preempted_by_turn)
        ├ active turn → 暂停 (active_turn)
        ├ backlog    → 暂停 (priority_backlog)
        ├ budget 耗尽 → 暂停 (budget_exhausted)
        └ 安全 → 执行单步 → 写 DriftCheckpointV1
       结束 → finish_drift(status=completed/paused/failed)
         └ 同步 drift_skill_state (per principal×skill)
```

## Kill Switch

| 关闭级别 | 操作 | 影响 |
|---|---|---|
| 全关 | `drift.enabled=false` | 停止创建新 drift.run Task |
| 停止创建 | Worker 不再领取新 drift.run；已有 Attempt 在安全点 checkpoint | 渐进收工 |
| 关闭 workspace Skill | `allow_workspace_skills=false` | 仅内置 Skill 运行 |
| 禁止发 Candidate | `allow_candidate_emission=false` | 仅内部 result |
| 单 Skill 暂停 | DB `drift_skill_state` 状态 / circuit | 不影响其它 Skill |

注意：`drift.enabled=false` **不静默删除历史**；已有 Attempt 在安全点写 Checkpoint（expand-first 策略）。

## 抢占（Preemption）

新 Turn 入站后置位 preemption signal（由 InboundService hook 触发）；Drift 在安全 step 检查并写 Checkpoint 暂停：

| 抢占原因 | 触发 |
|---|---|
| `active_turn` | 新 Turn running/queued |
| `preempted_by_turn` | explicit preemption signal |
| `lease_lost` | Lease 续租失败 |
| `priority_backlog` | 高优先级 Task backlog |
| `paused_budget_exhausted` | 当日 budget 用尽 |

抢占后 `drift_runs.status=paused`，`preemption_reason`，`result_ref` 指向最新 Checkpoint。

## 恢复（Resume）

下次 `drift.run` 被触发（或手动创建）且目标 `status=paused`：
1. 读 Checkpoint → 校验 `config_version_id` / `skill_version` / `checkpoint_schema_version`
2. 兼容 → 从 `step_index+1` 续跑（**budget 累计不重置**）
3. 不兼容 → `status=needs_review`（需人工处理）

## 现场观测（Dashboard / 表）

可直接查询的表/字段（不再返回占位值）：

- `drift_runs.status / preemption_reason / result_ref / steps_taken / budget_used_json`
- `drift_skill_state.last_status / last_run_at / run_count / checkpoint_ref`
- `drift_preemption_signals.preempt_requested`（实时抢占信号）
- `proactive_candidates` 中 `origin='drift'` 的 Candidate（Stage 3+）
- `tasks` 中 `task_type='drift.run'` 的行

## 手动控制

| 动作 | 方式 |
|---|---|
| enable/disable | `config.toml [drift] enabled` 走 Command API + Audit |
| run once | 手动创建 drift.run Task（幂等键含时间窗口） |
| pause Skill | DB `drift_skill_state` circuit / 配置 |
| reset circuit | DB `drift_skill_state` 清除 last_status=failed |
| approve operation | 高风险写操作转 Command/Approval |

## 回滚

- Migration 采用 expand-first；旧版本忽略新表/新列。`drift.enabled=false` 停止创建新 Task。
- 不删除 Drift 历史、Checkpoint、Receipt 或 Audit。
- `candidate_emission` 可独立关闭（内部只读维护仍运行）。

## 完成检查

| 检查项 | 状态 |
|---|---|
| 全量 Drift 测试通过 | ✅ |
| 新 Turn 在一个安全 step 内停止 Drift | ✅ |
| 崩溃恢复不重复副作用 | ✅ |
| Lease 丢失后零新副作用 | ✅ |
| 恢复后 budget 不重置 | ✅ |
| 默认安装不产生 Drift 外部副作用 | ✅ |
