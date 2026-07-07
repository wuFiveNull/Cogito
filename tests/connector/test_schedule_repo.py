"""Schedule / ScheduledFire 领域 + 表达式解析 + Repository 测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.schedule import (
    MisfirePolicy,
    Schedule,
    ScheduleStatus,
    ScheduleType,
    ScheduledFire,
    FireStatus,
    next_fire_at,
    parse_duration,
)
from cogito.store.migration import migrate
from cogito.store.schedule_repo import ScheduleRepository, ScheduledFireRepository


# ── 领域实体 ──

class TestScheduleEntity:
    def test_round_trip(self):
        now = datetime.now(UTC)
        s = Schedule(
            schedule_id="s1",
            schedule_type=ScheduleType.interval,
            expression="30m",
            timezone="Asia/Shanghai",
            next_fire_at=now,
            last_fire_at=now,
            connector_id="c1",
        )
        data = s.to_dict()
        assert data["schedule_id"] == "s1"
        assert data["schedule_type"] == "interval"

    def test_default_id(self):
        s = Schedule()
        assert len(s.schedule_id) == 32  # uuid4 hex


class TestScheduledFire:
    def test_default_status(self):
        f = ScheduledFire(schedule_id="s1", scheduled_fire_at=datetime.now(UTC))
        assert f.status == FireStatus.pending


# ── 表达式解析 ──

class TestParseDuration:
    def test_seconds(self):
        assert parse_duration("30s") == timedelta(seconds=30)

    def test_minutes(self):
        assert parse_duration("5m") == timedelta(minutes=5)

    def test_complex(self):
        assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)

    def test_days(self):
        assert parse_duration("1d6h") == timedelta(days=1, hours=6)

    def test_too_short_returns_none(self):
        assert parse_duration("10s") is None  # 最小 30s

    def test_invalid_returns_none(self):
        assert parse_duration("abc") is None
        assert parse_duration("") is None


class TestNextFireAt:
    BASE = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)

    def test_iso_timestamp(self):
        dt = next_fire_at("2026-07-15T09:00:00Z", "UTC", self.BASE)
        assert dt is not None
        assert dt.day == 15
        assert dt.hour == 9

    def test_every_duration_hours(self):
        dt = next_fire_at("every 2h", "UTC", self.BASE)
        assert dt == self.BASE + timedelta(hours=2)

    def test_every_duration_minutes(self):
        dt = next_fire_at("every 30m", "UTC", self.BASE)
        assert dt == self.BASE + timedelta(minutes=30)

    def test_every_day_time(self):
        dt = next_fire_at("every day 08:00", "UTC", self.BASE)
        assert dt is not None
        assert dt.hour == 8 and dt.minute == 0
        # BASE 12:00 → 次日 08:00
        assert dt.day == self.BASE.day + 1

    def test_every_weekly(self):
        dt = next_fire_at("every monday 9am", "UTC", self.BASE)
        assert dt is not None
        assert dt.weekday() == 0  # Monday
        assert dt.hour == 9

    def test_every_weekly_with_pm(self):
        dt = next_fire_at("every friday 6pm", "UTC", self.BASE)
        assert dt is not None
        assert dt.weekday() == 4  # Friday
        assert dt.hour == 18

    def test_every_1d(self):
        dt = next_fire_at("every 1d", "UTC", self.BASE)
        assert dt == self.BASE + timedelta(days=1)

    def test_duration(self):
        dt = next_fire_at("1h30m", "UTC", self.BASE)
        assert dt == self.BASE + timedelta(hours=1, minutes=30)

    def test_cron_weekdays_at_9(self):
        # 工作日 09:00
        dt = next_fire_at("0 9 * * 1-5", "UTC", self.BASE)
        assert dt is not None
        assert dt.hour == 9 and dt.minute == 0
        assert dt.weekday() < 5  # Mon-Fri

    def test_cron_every_30_min(self):
        dt = next_fire_at("*/30 * * * *", "UTC", self.BASE)
        assert dt is not None
        assert dt.minute in (0, 30)

    def test_invalid_expression_returns_none(self):
        assert next_fire_at("not-a-valid-expr", "UTC", self.BASE) is None
        assert next_fire_at("1 2 3", "UTC", self.BASE) is None  # 不是 5 field


# ── Repository ──

class TestScheduleRepository:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def repo(self, conn):
        return ScheduleRepository(conn)

    def test_insert_and_get(self, repo):
        now = datetime.now(UTC)
        s = Schedule(
            schedule_id="s1",
            expression="30m",
            next_fire_at=now,
            connector_id="c1",
        )
        repo.insert(s)
        got = repo.get("s1")
        assert got is not None
        assert got.expression == "30m"
        assert got.connector_id == "c1"
        assert got.next_fire_at is not None

    def test_find_due(self, repo):
        now = datetime.now(UTC)
        s1 = Schedule(schedule_id="s1", next_fire_at=now - timedelta(minutes=1), enabled=True)
        s2 = Schedule(schedule_id="s2", next_fire_at=now + timedelta(hours=1), enabled=True)
        s3 = Schedule(schedule_id="s3", next_fire_at=now - timedelta(minutes=1), enabled=False)
        for s in (s1, s2, s3):
            repo.insert(s)
        due = repo.find_due(now)
        ids = {s.schedule_id for s in due}
        assert "s1" in ids
        assert "s2" not in ids  # 未到期
        assert "s3" not in ids  # disabled

    def test_update_fire_time_optimistic_lock(self, repo):
        now = datetime.now(UTC)
        s = Schedule(schedule_id="s1", next_fire_at=now, version=1)
        repo.insert(s)
        # 版本号匹配 → 成功
        ok = repo.update_fire_time("s1", now + timedelta(hours=1), now, expected_version=1)
        assert ok
        # 版本号不匹配 → 失败
        ok = repo.update_fire_time("s1", now + timedelta(hours=2), now, expected_version=1)
        assert not ok


class TestScheduledFireRepository:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def repo(self, conn):
        return ScheduledFireRepository(conn)

    @pytest.fixture
    def schedule(self, conn):
        s = Schedule(schedule_id="s1", expression="30m")
        ScheduleRepository(conn).insert(s)
        return s

    def test_insert_and_find(self, repo, schedule):
        now = datetime.now(UTC)
        fire = ScheduledFire(schedule_id="s1", scheduled_fire_at=now, status=FireStatus.fired)
        repo.insert(fire)
        got = repo.find("s1", now)
        assert got is not None
        assert got.status == FireStatus.fired
        assert got.schedule_id == "s1"

    def test_update_status(self, repo, schedule):
        now = datetime.now(UTC)
        fire = ScheduledFire(schedule_id="s1", scheduled_fire_at=now)
        repo.insert(fire)
        repo.update_status(fire.fire_id, FireStatus.fired, task_id="t1")
        got = repo.find("s1", now)
        assert got.status == FireStatus.fired
        assert got.task_id == "t1"

    def test_idempotent_find(self, repo, schedule):
        now = datetime.now(UTC)
        assert repo.find("missing", now) is None
