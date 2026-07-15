"""M2: ProactiveCadencePolicy 自适应调度测试。

- energy band → interval 映射（高能量短、低能量长）
- jitter 由注入 RNG 控制，可复现
- 上下限裁剪
- Scheduler cadence state：未到期不创建任务、到期创建单次、misfire 最多一次
"""
from __future__ import annotations

import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from cogito.config import ProactiveCadenceConfig
from cogito.domain.task import Task, TaskStatus
from cogito.service.energy_model import compute_energy
from cogito.service.proactive_cadence import compute_interval
from cogito.service.presence import SqlitePresenceReader
from cogito.store.migration import migrate

from cogito.domain.schedule import MisfirePolicy  # noqa: F401 (sanity import)


# ── fixtures ──


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def memory_db():
    conn = _fresh_db()
    yield conn
    conn.close()


def _cadence(**over) -> ProactiveCadenceConfig:
    base = dict(
        min_interval_seconds=60, max_interval_seconds=1800,
        high_energy_interval_seconds=60, medium_energy_interval_seconds=240,
        low_energy_interval_seconds=480, jitter_ratio=0.10,
    )
    base.update(over)
    return ProactiveCadenceConfig(**base)


# ── compute_interval 纯函数 ──


class TestComputeInterval:
    def test_high_energy_short(self):
        # 高能量 = high_energy_interval_seconds（无 jitter 时）
        cfg = _cadence(jitter_ratio=0.0)
        assert compute_interval("high", cfg) == cfg.high_energy_interval_seconds

    def test_low_energy_long(self):
        cfg = _cadence(jitter_ratio=0.0)
        assert compute_interval("low", cfg) == cfg.low_energy_interval_seconds

    def test_unknown_band_defaults_medium(self):
        cfg = _cadence(jitter_ratio=0.0)
        assert compute_interval("weird", cfg) == cfg.medium_energy_interval_seconds

    def test_clamps_to_min(self):
        cfg = _cadence(
            min_interval_seconds=100, max_interval_seconds=1800,
            high_energy_interval_seconds=10, jitter_ratio=0.0,
        )
        assert compute_interval("high", cfg) == 100

    def test_clamps_to_max(self):
        cfg = _cadence(
            min_interval_seconds=60, max_interval_seconds=100,
            low_energy_interval_seconds=5000, jitter_ratio=0.0,
        )
        assert compute_interval("low", cfg) == 100

    def test_jitter_in_range(self):
        cfg = _cadence(jitter_ratio=0.10, jitter_applied=True) if False else _cadence(jitter_ratio=0.10)
        base = cfg.medium_energy_interval_seconds
        rng = random.Random(42)
        for _ in range(50):
            iv = compute_interval("medium", cfg, rng=rng)
            assert cfg.min_interval_seconds <= iv <= cfg.max_interval_seconds
            # jitter ±10% → [base*0.9, base*1.1]
            assert int(base * 0.9) - 1 <= iv <= int(base * 1.1) + 1

    def test_injected_rng_reproducible(self):
        cfg = _cadence(jitter_ratio=0.10)
        r1 = random.Random(7)
        r2 = random.Random(7)
        a = [compute_interval("medium", cfg, rng=r1) for _ in range(10)]
        b = [compute_interval("medium", cfg, rng=r2) for _ in range(10)]
        assert a == b


# ── Scheduler tick cadence ──


class TestSchedulerCadence:
    def test_first_tick_creates_task_and_persists_next(self, memory_db):
        """首次 tick（next_eval_at=0 到期）须创建 Task 并写入 next_eval_at。"""
        conn = memory_db
        from cogito.config import ProactiveConfig
        from cogito.service.scheduler import Scheduler
        cfg = ProactiveConfig(enabled=True, dry_run=True)
        tasks = TaskListScheduler(conn, proactive_config=cfg).tick_proactive_evaluate()
        assert len(tasks) == 1
        state = conn.execute(
            "SELECT next_eval_at, interval_s, energy_band "
            "FROM proactive_cadence_state WHERE id=1"
        ).fetchone()
        assert state["next_eval_at"] > 0
        assert state["interval_s"] == pytest.approx(240, abs=200)  # medium band fallback

    def test_subsequent_tick_before_due_is_idle(self, memory_db):
        """未到期时第二次 tick 不创建任务。"""
        conn = memory_db
        from cogito.config import ProactiveConfig
        cfg = ProactiveConfig(enabled=True, dry_run=True)
        sched = TaskListScheduler(conn, proactive_config=cfg)
        first = sched.tick_proactive_evaluate()
        assert len(first) == 1
        # 未到 next_eval_at，立即再 tick
        second = sched.tick_proactive_evaluate()
        assert len(second) == 0

    def test_tick_after_due_fires_again(self, memory_db):
        """推进 clock 过 next_eval_at 后再次触发。"""
        conn = memory_db
        from cogito.config import ProactiveConfig
        from cogito.contracts.clock import FakeClock
        cfg = ProactiveConfig(enabled=True, dry_run=True)
        start = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
        clock = FakeClock(start)
        sched = TaskListScheduler(
            conn, clock=clock, proactive_config=cfg,)
        first = sched.tick_proactive_evaluate()
        assert len(first) == 1
        # 推进超过 interval_s
        state = conn.execute(
            "SELECT interval_s FROM proactive_cadence_state WHERE id=1"
        ).fetchone()
        clock.advance(seconds=state["interval_s"] + 5)
        second = sched.tick_proactive_evaluate()
        assert len(second) == 1

    def test_disabled_proactive_no_task(self, memory_db):
        """proactive.enabled=false 时不创建任务。"""
        conn = memory_db
        from cogito.config import ProactiveConfig
        cfg = ProactiveConfig(enabled=False)
        sched = TaskListScheduler(conn, proactive_config=cfg)
        assert sched.tick_proactive_evaluate() == []

    def test_energy_band_drives_interval(self, memory_db):
        """按 PROACTIVE-IDLE 3.3：高能量降低主动性，低能量提高主动性。"""
        conn = memory_db
        from cogito.config import ProactiveConfig
        cfg = ProactiveConfig(enabled=True, dry_run=True)

        # high band
        class HighReader:
            def get_last_user_activity(self, principal_id):
                return datetime.now(UTC) - timedelta(minutes=1)

        sched_high = TaskListScheduler(conn, proactive_config=cfg, presence_reader=HighReader())
        sched_high.tick_proactive_evaluate()
        high_iv = conn.execute(
            "SELECT interval_s FROM proactive_cadence_state WHERE id=1"
        ).fetchone()[0]

        # 重置 state
        conn.execute("UPDATE proactive_cadence_state SET next_eval_at=0, id=1 WHERE id=1")
        conn.execute("DELETE FROM tasks")
        conn.commit()

        # low band (无活动)
        class LowReader:
            def get_last_user_activity(self, principal_id):
                return None

        sched_low = TaskListScheduler(conn, proactive_config=cfg, presence_reader=LowReader())
        sched_low.tick_proactive_evaluate()
        low_iv = conn.execute(
            "SELECT interval_s FROM proactive_cadence_state WHERE id=1"
        ).fetchone()[0]

        assert high_iv > low_iv


class TaskListScheduler:
    """测试用的薄封装 Scheduler，仅暴露 proactive cadence tick。"""

    def __init__(self, conn, clock=None, proactive_config=None, presence_reader=None):
        from cogito.service.scheduler import Scheduler
        self._sched = Scheduler(
            conn, clock=clock, proactive_config=proactive_config,
            presence_reader=presence_reader,
        )

    def tick_proactive_evaluate(self):
        return self._sched.tick_proactive_evaluate()
