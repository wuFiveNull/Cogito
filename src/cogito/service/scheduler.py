"""Scheduler —— 周期触发到期 schedule，生成 connector.poll Task。

每次 tick：
1. 查 due schedules（enabled AND next_fire_at <= now）
2. 逐条 claim schedule lease（条件更新版本号防并发）
3. 幂等创建 ScheduledFire
4. 创建 connector.poll Task（payload_ref 指向 connector_id）
5. 更新 next_fire_at（按表达式计算下次）并释放 lease
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timedelta

from cogito.domain.schedule import (
    FireStatus,
    MisfirePolicy,
    Schedule,
    ScheduledFire,
    next_fire_at,
)
from cogito.domain.task import Task, TaskStatus
from cogito.contracts.clock import Clock, ProductionClock
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.schedule_repo import ScheduledFireRepository, ScheduleRepository
from cogito.store.task_repo import TaskRepository
from cogito.contracts.clock import epoch_ms

_LOGGER = logging.getLogger(__name__)

POLL_TASK_TYPE = "connector.poll"
MCP_POLL_TASK_TYPE = "mcp_connector.poll"
PROACTIVE_EVALUATE_TASK_TYPE = "proactive.evaluate"
PROACTIVE_DIGEST_PUBLISH_TASK_TYPE = "proactive.digest.publish"
MEMORY_RECOMPUTE_WEIGHT_TASK_TYPE = "memory.recompute_weight"
MEMORY_CONSOLIDATE_TASK_TYPE = "memory.consolidate"
DRIFT_RUN_TASK_TYPE = "drift.run"


class Scheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock | None = None,
        proactive_config: Any = None,     # ProactiveConfig
        presence_reader: Any = None,       # PresenceReader
        rng: Any = None,                   # random.Random (可注入，测试可复现)
        drift_config: Any = None,          # DriftConfig
        config_version_id: str = "",
    ) -> None:
        self._conn = conn
        self._clock = clock or ProductionClock()
        self._schedule_repo = ScheduleRepository(conn)
        self._fire_repo = ScheduledFireRepository(conn)
        self._task_repo = TaskRepository(conn)
        self._proactive_config = proactive_config
        self._presence_reader = presence_reader
        self._rng = rng
        self._drift_config = drift_config          # DriftConfig
        self._config_version_id = config_version_id

    @staticmethod
    def task_type_for_connector(connector_type: str) -> str:
        """根据 Connector 类型分派 Task 类型。

        RSS/Atom/JSON → connector.poll（现有 RSS 流程）
        MCP → mcp_connector.poll（本计划新增）
        """
        if connector_type == "mcp":
            return MCP_POLL_TASK_TYPE
        return POLL_TASK_TYPE

    def _try_create_unique_task(self, task_type: str, idempotency: str,
                                 payload_ref: str = "", *, priority: int = 20,
                                 origin: str = "memory_maintenance") -> Task | None:
        """幂等：创建唯一 Task（已存在则跳过），返回 Task 或 None。"""
        existing = self._conn.execute(
            "SELECT task_id FROM tasks WHERE idempotency_key=?", (idempotency,),
        ).fetchone()
        if existing is not None:
            return None
        task = Task(
            task_id=f"task-mm-{idempotency}",
            task_type=task_type,
            payload_ref=payload_ref,
            status=TaskStatus.queued,
            priority=priority,
            idempotency_key=idempotency,
            origin=origin,
        )
        try:
            self._task_repo.insert(task)
            self._conn.commit()
            return task
        except Exception:
            self._conn.rollback()
            _LOGGER.debug("unique task %s insert skipped (duplicate race)", task_type)
            return None

    def tick_memory_maintenance(self) -> list[Task]:
        """PLAN-14 R-06: 定期创建 memory.recompute_weight / memory.consolidate 任务。

        每次 process_background_once 调用一次。幂等键基于任务类型 + 时间窗口，
        窗口内不重复创建。窗口大小由配置决定（默认 600s recompute / 3600s consolidate）。
        """
        now = self._now()
        now_ms = epoch_ms(now)
        tasks: list[Task] = []

        # memory.recompute_weight —— 窗口：每 600s（10 分钟）一次
        recompute_window = now_ms // (10 * 60 * 1000)
        idemp_recompute = f"memory-maintenance:recompute:{recompute_window}"
        t = self._try_create_unique_task(
            MEMORY_RECOMPUTE_WEIGHT_TASK_TYPE, idemp_recompute,
            priority=10, origin="memory_maintenance",
        )
        if t:
            tasks.append(t)

        # memory.consolidate —— 窗口：每 3600s（1 小时）一次
        consolidate_window = now_ms // (60 * 60 * 1000)
        idemp_consolidate = f"memory-maintenance:consolidate:{consolidate_window}"
        t = self._try_create_unique_task(
            MEMORY_CONSOLIDATE_TASK_TYPE, idemp_consolidate,
            priority=5, origin="memory_maintenance",
        )
        if t:
            tasks.append(t)

        return tasks

    def tick_proactive_evaluate(self, limit: int = 10) -> list[Task]:
        """主动评估 tick：energy-driven 自适应节拍。

        不再每次 process_background_once 都创建 Task，而是读取
        proactive_cadence_state.next_eval_at，仅在到期 (now >= next_eval_at) 时
        创建 proactive.evaluate Task 并按当前 energy band 计算下一次触发。

        - 可注入 Clock/RNG（构造时注入，测试可复现）。
        - misfire coalesce：到期时无论错过了多少 tick 只补一次评估。
        - Alert 由 Event 立即触发 (schedule_immediate_evaluate)，不走此节流。
        """
        if self._proactive_config is None or not self._proactive_config.enabled:
            return []
        now = self._now()
        now_ms = epoch_ms(now)

        state = self._read_cadence_state()
        # 未到期 → 不创建任务
        if state["next_eval_at"] > now_ms:
            return []

        tasks: list[Task] = []
        # 创建 proactive.evaluate 任务（单次，misfire 只补一次）
        idempotency = f"proactive-evaluate:{now_ms}"
        existing = self._conn.execute(
            "SELECT task_id FROM tasks WHERE idempotency_key=?", (idempotency,),
        ).fetchone()
        if existing is None:
            # Task.scheduled_at / created_at 为 datetime 类型（task_repo.insert
            # 内部调用 epoch_ms() 转为 epoch ms 存储）。
            task = Task(
                task_id=f"task-pe-{idempotency}",
                task_type=PROACTIVE_EVALUATE_TASK_TYPE,
                payload_ref="",
                status=TaskStatus.queued,
                priority=15,
                scheduled_at=now,
                idempotency_key=idempotency,
                origin="proactive-scheduler",
            )
            try:
                self._task_repo.insert(task)
                tasks.append(task)
            except Exception:
                self._conn.rollback()
                _LOGGER.warning("insert proactive.evaluate task failed (likely duplicate)")
                return tasks

        # 根据当前能量档计算下一次评估间隔
        band = self._current_energy_band()
        interval_s = self._compute_cadence_interval(band)
        next_eval_at = now_ms + interval_s * 1000
        self._write_cadence_state(
            last_eval_at=now_ms,
            next_eval_at=next_eval_at,
            interval_s=interval_s,
            energy_band=band,
            updated_at=now_ms,
        )
        try:
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            _LOGGER.warning("persist cadence state failed")
        return tasks

    def tick_drift_admit(self) -> tuple[str, str] | None:
        """Drift admission tick（M3）。

        全局 idle 检查；admit 时创建幂等 drift.run Task (origin=drift-admission)。
        dry_run 仅记录应选 Skill，不创建 Task。
        返回 (drift_run_id, task_id) 或 None。
        """
        if self._drift_config is None or not self._drift_config.enabled:
            return None
        from cogito.service.drift_admission import admit
        result = admit(
            self._conn,
            principal_id=self._drift_config.default_principal_id,
            idle_after_minutes=self._drift_config.idle_after_minutes,
            max_runs_per_day=self._drift_config.max_runs_per_day,
            max_concurrent=self._drift_config.max_concurrent,
            presence_reader=self._presence_reader,
        )
        if not result.admit:
            _LOGGER.debug("drift admission denied: %s", result.reasons)
            return None

        # dry-run：仅记录，不创建真实 Task
        if self._drift_config.dry_run:
            _LOGGER.info("[dry_run] drift admission would select skill (snapshot=%s)",
                         result.snapshot.to_dict())
            return None

        # 创建幂等 drift.run Task (idempotency_key 包含 snapshot_at 的粗粒度窗口)
        now = self._now()
        now_ms = epoch_ms(now)
        # 粗粒度幂等窗口（1 分钟内只创建一个）
        window_ms = (now_ms // 60000) * 60000
        idempotency = f"drift-run:{self._drift_config.default_principal_id}:{window_ms}"
        existing = self._conn.execute(
            "SELECT task_id FROM tasks WHERE idempotency_key=?", (idempotency,),
        ).fetchone()
        if existing is not None:
            return None

        from cogito.domain.task import Task, TaskStatus
        from cogito.store.drift_repo import DriftRunRepository

        task = Task(
            task_id=f"task-dr-{uuid.uuid4().hex[:16]}",
            task_type=DRIFT_RUN_TASK_TYPE,
            payload_ref="",
            status=TaskStatus.queued,
            priority=5,  # 低于 proactive.evaluate (15) 和 memory (10)
            scheduled_at=now,
            idempotency_key=idempotency,
            origin="drift-admission",
        )
        try:
            self._task_repo.insert(task)
        except Exception:
            self._conn.rollback()
            _LOGGER.warning("insert drift.run task failed")
            return None

        # 写 drift_runs 记录
        repo = DriftRunRepository(self._conn)
        try:
            run_id = repo.insert(
                task_id=task.task_id,
                principal_id=self._drift_config.default_principal_id,
                skill_name="(selected-at-run)",
                skill_version="1.0",
                admission_snapshot=result.snapshot.to_dict(),
            )
        except Exception:
            self._conn.rollback()
            _LOGGER.warning("insert drift_run record failed")
            return None

        # 释放由 Scheduler claim 的 Lease（Scheduler 不 claim；由 Task Worker 以后领取）
        # 只 commit Task + drift_run
        try:
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            return None
        _LOGGER.info("drift admitted: run=%s task=%s", run_id, task.task_id)
        return (run_id, task.task_id)

    def _now(self) -> datetime:
        return self._clock.now()

    # ── M2: cadence state 读写 + 能量档 + 间隔计算 ──

    def _read_cadence_state(self) -> dict[str, Any]:
        """读 cadence 单例行；不存在则返回到期默认。"""
        row = self._conn.execute(
            "SELECT last_eval_at, next_eval_at, interval_s, energy_band "
            "FROM proactive_cadence_state WHERE id=1",
        ).fetchone()
        if row is None:
            return {
                "last_eval_at": None, "next_eval_at": 0,
                "interval_s": 60, "energy_band": "medium",
            }
        return {
            "last_eval_at": row[0], "next_eval_at": row[1],
            "interval_s": row[2], "energy_band": row[3],
        }

    def _write_cadence_state(
        self, *, last_eval_at, next_eval_at, interval_s, energy_band, updated_at,
    ) -> None:
        self._conn.execute(
            "UPDATE proactive_cadence_state "
            "SET last_eval_at=?, next_eval_at=?, interval_s=?, "
            "energy_band=?, updated_at=? WHERE id=1",
            (last_eval_at, next_eval_at, interval_s, energy_band, updated_at),
        )

    def _current_energy_band(self) -> str:
        """取当前能量档：有 PresenceReader 用真实活动，否则 medium。"""
        if self._presence_reader is None or self._proactive_config is None:
            return "medium"
        from cogito.service.energy_model import compute_energy, energy_band
        try:
            last_user_dt = self._presence_reader.get_last_user_activity(
                self._proactive_config.default_principal_id,
            )
        except Exception:
            return "medium"
        return energy_band(compute_energy(last_user_dt))

    def _compute_cadence_interval(self, band: str) -> int:
        """按能量档 + 配置计算下一次评估间隔（秒，含 jitter、上下限）。"""
        from cogito.service.proactive_cadence import compute_interval
        if self._proactive_config is None:
            return 60
        return compute_interval(
            band, self._proactive_config.cadence, rng=self._rng,
        )

    def tick(self, limit: int = 10) -> list[Task]:
        """执行一轮调度，返回创建的 Task 列表。"""
        now = self._now()
        due = self._schedule_repo.find_due(now, limit=limit)
        created: list[Task] = []

        for schedule in due:
            try:
                if self._is_misfired(schedule, now):
                    tasks = self._handle_misfire(schedule, now)
                else:
                    task = self._process_schedule(schedule, now)
                    tasks = [task] if task is not None else []
                created.extend(tasks)
            except Exception:
                _LOGGER.exception("Scheduler: failed to process %s", schedule.schedule_id)

        return created

    def _is_misfired(self, schedule: Schedule, now: datetime) -> bool:
        """判断 schedule 是否处于错过触发状态。

        当 now - last_fired_at > 1.5 * interval 时判定为 misfire。
        """
        if schedule.last_fire_at is None:
            return False
        interval = self._estimate_interval(schedule)
        if interval is None or interval <= 0:
            return False
        gap = (now - schedule.last_fire_at).total_seconds()
        return gap > interval * 1.5

    def _estimate_interval(self, schedule: Schedule) -> float | None:
        """估算 schedule 的触发间隔（秒）。

        优先使用 normalized_interval_s；否则从 expression 解析。
        """
        # 尝试从 schedule 的规范化字段获取
        row = self._conn.execute(
            "SELECT normalized_interval_s FROM schedules WHERE schedule_id=?",
            (schedule.schedule_id,),
        ).fetchone()
        if row and row[0]:
            return float(row[0])
        # 回退：从 expression 解析
        from cogito.domain.schedule import parse_duration
        delta = parse_duration(schedule.expression)
        if delta is not None:
            return delta.total_seconds()
        return None

    def _handle_misfire(self, schedule: Schedule, now: datetime) -> list[Task]:
        """按 misfire_policy 处理错过的触发。

        返回创建的 Task 列表（可能多个，也可能合并为一个）。
        """
        policy = schedule.misfire_policy
        interval = self._estimate_interval(schedule)
        last = schedule.last_fire_at or schedule.created_at

        if interval is None or interval <= 0:
            # 无法计算间隔：只触发一次
            task = self._process_schedule(schedule, now)
            return [task] if task is not None else []

        gap = (now - last).total_seconds()
        missed_count = max(1, int(gap / interval))

        if missed_count <= 1:
            task = self._process_schedule(schedule, now)
            return [task] if task is not None else []

        if policy in (MisfirePolicy.skip, MisfirePolicy.run_once):
            # 只补一次当前触发
            task = self._process_schedule(schedule, now)
            return [task] if task is not None else []

        if policy == MisfirePolicy.catch_up_limited:
            n = min(missed_count, schedule.max_catch_up)
            tasks: list[Task] = []
            for i in range(1, n + 1):
                fire_at = last + timedelta(seconds=interval * i)
                # 每次触发后重新获取 schedule（version 已变）
                fresh = self._schedule_repo.get(schedule.schedule_id)
                if fresh is None:
                    break
                task = self._process_schedule(fresh, now, fire_at=fire_at)
                if task is not None:
                    tasks.append(task)
            return tasks

        if policy == MisfirePolicy.merge:
            # 合并为一个 Task，payload 携带合并元数据
            task = self._process_schedule(schedule, now, merged_count=missed_count)
            return [task] if task is not None else []

        # 默认：只触发一次
        task = self._process_schedule(schedule, now)
        return [task] if task is not None else []

    def _process_schedule(
        self,
        schedule: Schedule,
        now: datetime,
        fire_at: datetime | None = None,
        merged_count: int = 0,
    ) -> Task | None:
        """处理单条到期 schedule。返回创建的 Task，或 None（跳过）。

        Args:
            fire_at: 显式指定触发时间（misfire 补触发时使用）。
            merged_count: 合并触发的次数（merge 策略 > 1 时写入 payload）。
        """
        fire_at = fire_at or schedule.next_fire_at or now

        # 幂等：此 fire_at 是否已触发过
        existing = self._fire_repo.find(schedule.schedule_id, fire_at)
        if existing is not None and existing.status == FireStatus.fired:
            # 已触发但仍出现在 due 列表 —— 推进到下次
            nxt = next_fire_at(schedule.expression, schedule.timezone, now)
            if nxt and nxt != schedule.next_fire_at:
                self._schedule_repo.update_fire_time(
                    schedule.schedule_id, nxt, now, schedule.version,
                )
            return None

        # Claim schedule lease: 条件更新版本号（将 next_fire_at 暂设为自身以锁定）
        if not self._schedule_repo.update_fire_time(
            schedule.schedule_id, schedule.next_fire_at, now, schedule.version,
        ):
            return None  # 并发竞争失败，跳过

        with UnitOfWork(self._conn) as uow:
            # 创建 fire 记录
            fire = ScheduledFire(
                schedule_id=schedule.schedule_id,
                scheduled_fire_at=fire_at,
                status=FireStatus.fired,
            )
            self._fire_repo.insert(fire)

            # 计算下次触发时间
            nxt = next_fire_at(schedule.expression, schedule.timezone, now)

            # 选择 task_type：按 connector_type 分派（mcp vs rss/json/atom）
            task_type = POLL_TASK_TYPE
            if schedule.connector_id:
                row = self._conn.execute(
                    "SELECT connector_type FROM connectors WHERE connector_id=?",
                    (schedule.connector_id,),
                ).fetchone()
                if row is not None:
                    task_type = self.task_type_for_connector(row[0])

            # 构建 payload：merge 时携带合并元数据
            payload_ref = schedule.connector_id or ""
            if merged_count > 1:
                # merge 策略：payload 携带合并次数和时间窗口
                import json
                payload_ref = json.dumps({
                    "connector_id": schedule.connector_id or "",
                    "merged_count": merged_count,
                    "first_missed_at": (fire_at.isoformat() if fire_at else None),
                    "last_missed_at": (now.isoformat() if now else None),
                })

            # 创建 connector.poll / mcp_connector.poll Task
            task = Task(
                task_type=task_type,
                payload_ref=payload_ref,
                status=TaskStatus.queued,
                priority=40,
                scheduled_at=nxt,
                idempotency_key=f"{schedule.schedule_id}:{epoch_ms(fire_at)}",
                origin="scheduler",
            )
            self._task_repo.insert(task)

            # 更新 schedule 的 next_fire_at + last_fired_at（version 再 +1）
            self._conn.execute(
                "UPDATE schedules SET next_fire_at=?, last_fired_at=?, version=version+1 "
                "WHERE schedule_id=?",
                (epoch_ms(nxt), epoch_ms(fire_at), schedule.schedule_id),
            )

            # 回填 fire.task_id
            self._fire_repo.update_status(
                fire.fire_id, FireStatus.fired, task.task_id,
            )

            uow.commit()

        _LOGGER.info(
            "Scheduler: schedule=%s fired at %s, next=%s, task=%s",
            schedule.schedule_id, fire_at.isoformat(), nxt.isoformat() if nxt else None,
            task.task_id,
        )
        return task
