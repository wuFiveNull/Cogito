"""Schedule / ScheduledFire 数据访问层。

持久化调度配置和触发记录（幂等键 schedule_id + scheduled_fire_at）。
Event-only：读路径由 replay_schedule 重建，写路径只追加 Event。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from cogito.contracts.clock import epoch_ms, from_epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.schedule import (
    FireStatus,
    Schedule,
    ScheduleType,
    ScheduledFire,
)
from cogito.store.event_replay import ScheduleProjection, replay_schedule
from cogito.store.event_store import EventStore

_SCHEDULE_STREAM_TYPE = "schedule"


def _projection_to_schedule(p: ScheduleProjection) -> Schedule:
    """Convert a ScheduleProjection back to the domain Schedule object."""
    return Schedule(
        schedule_id=p.schedule_id,
        schedule_type=p.schedule_type,
        expression=p.expression,
        timezone=p.timezone,
        misfire_policy=p.misfire_policy,
        max_catch_up=p.max_catch_up or 3,
        enabled=p.enabled,
        next_fire_at=from_epoch_ms(p.next_fire_at) if p.next_fire_at else None,
        last_fire_at=from_epoch_ms(p.last_fire_at) if p.last_fire_at else None,
        version=p.version or 1,
        connector_id=p.connector_id or None,
        created_at=from_epoch_ms(p.created_at) if p.created_at else None,
        task_type=p.task_type or "connector.poll",
        task_payload=p.task_payload or "",
    )


class ScheduleRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._event_store = EventStore(conn)

    def get(self, schedule_id: str) -> Schedule | None:
        events = self._event_store.read_stream(
            _SCHEDULE_STREAM_TYPE, schedule_id
        )
        projection = replay_schedule(events, schedule_id)
        return _projection_to_schedule(projection) if projection else None

    def insert(self, schedule: Schedule) -> None:
        normalized_s = self._compute_interval(schedule.expression)
        self._event_store.append(
            Event(
                event_type="schedule.created",
                stream_type=_SCHEDULE_STREAM_TYPE,
                stream_id=schedule.schedule_id,
                producer="schedule-repository",
                event_class=EventClass.DOMAIN,
                summary=f"Schedule {schedule.schedule_type.value} created",
                attributes={
                    "schedule_type": schedule.schedule_type.value,
                    "expression": schedule.expression,
                    "timezone": schedule.timezone,
                    "misfire_policy": schedule.misfire_policy.value,
                    "max_catch_up": schedule.max_catch_up,
                    "enabled": schedule.enabled,
                    "connector_id": schedule.connector_id or "",
                    "task_type": schedule.task_type,
                    "task_payload": schedule.task_payload or "",
                    "next_fire_at": epoch_ms(schedule.next_fire_at) if schedule.next_fire_at else None,
                    "last_fire_at": epoch_ms(schedule.last_fire_at) if schedule.last_fire_at else None,
                    "normalized_interval_s": normalized_s,
                },
                outcome="active",
                idempotency_key=f"schedule:{schedule.schedule_id}:created",
            ),
            expected_version=0,
        )

    @staticmethod
    def _compute_interval(expression: str) -> int | None:
        """从 expression 估算触发间隔（秒）。"""
        from cogito.domain.schedule import parse_duration

        delta = parse_duration(expression.strip())
        if delta is not None:
            return int(delta.total_seconds())
        return None

    def _event_schedules(self) -> list[Schedule]:
        """Replay all schedule streams and return the current state of each."""
        grouped: dict[str, list[Event]] = {}
        for event in self._event_store.read_stream_type(_SCHEDULE_STREAM_TYPE):
            grouped.setdefault(event.stream_id, []).append(event)
        result: list[Schedule] = []
        for sid, stream in grouped.items():
            projection = replay_schedule(stream, sid)
            if projection is not None:
                result.append(_projection_to_schedule(projection))
        return result

    def find_due(self, now: datetime, limit: int = 10) -> list[Schedule]:
        """查找已到期的 enabled schedules 通过 Event replay。"""
        now_ms = epoch_ms(now)
        due = [
            s
            for s in self._event_schedules()
            if s.enabled
            and s.next_fire_at is not None
            and epoch_ms(s.next_fire_at) <= now_ms
        ]
        due.sort(key=lambda s: s.next_fire_at or datetime.max.replace(tzinfo=UTC))
        return due[:limit]

    def find_all(self, limit: int = 100) -> list[Schedule]:
        schedules = self._event_schedules()
        schedules.sort(key=lambda s: s.created_at or datetime.min.replace(tzinfo=UTC))
        return schedules[:limit]

    def update_fire_time(
        self,
        schedule_id: str,
        next_fire_at: datetime | None,
        last_fire_at: datetime | None,
        expected_version: int,
    ) -> bool:
        """条件更新触发时间（乐观锁）。返回 True 表示 Event 追加成功（版本校验通过）。"""
        try:
            self._event_store.append(
                Event(
                    event_type="schedule.fired",
                    stream_type=_SCHEDULE_STREAM_TYPE,
                    stream_id=schedule_id,
                    producer="schedule-repository",
                    event_class=EventClass.OPERATION,
                    summary="Schedule fired",
                    attributes={
                        "next_fire_at": epoch_ms(next_fire_at) if next_fire_at else None,
                        "last_fire_at": epoch_ms(last_fire_at) if last_fire_at else None,
                        "expected_version": expected_version,
                    },
                    outcome="fired",
                    idempotency_key=(
                        f"schedule:{schedule_id}:fired:"
                        f"{epoch_ms(next_fire_at) if next_fire_at else 'none'}"
                    ),
                ),
                expected_version=expected_version,
            )
            return True
        except Exception:
            return False

    def update_enabled(self, schedule_id: str, enabled: bool) -> None:
        """Toggle enabled state via Event append."""
        # Read current stream version to use as expected_version
        events = self._event_store.read_stream(_SCHEDULE_STREAM_TYPE, schedule_id)
        projection = replay_schedule(events, schedule_id)
        expected_version = projection.stream_version if projection else 0
        self._event_store.append(
            Event(
                event_type="schedule.enabled_toggled",
                stream_type=_SCHEDULE_STREAM_TYPE,
                stream_id=schedule_id,
                producer="schedule-repository",
                event_class=EventClass.OPERATION,
                summary=f"Schedule {'enabled' if enabled else 'disabled'}",
                attributes={"enabled": enabled},
                outcome="active" if enabled else "disabled",
                idempotency_key=f"schedule:{schedule_id}:enabled:{enabled}",
            ),
            expected_version=expected_version,
        )

    def update_enabled_expected(
        self,
        schedule_id: str,
        enabled: bool,
        expected_version: int,
    ) -> bool:
        """Toggle enabled with caller-supplied expected_version. 返回 True 表示成功。"""
        try:
            self._event_store.append(
                Event(
                    event_type="schedule.enabled_toggled",
                    stream_type=_SCHEDULE_STREAM_TYPE,
                    stream_id=schedule_id,
                    producer="schedule-repository",
                    event_class=EventClass.OPERATION,
                    summary=f"Schedule {'enabled' if enabled else 'disabled'}",
                    attributes={"enabled": enabled},
                    outcome="active" if enabled else "disabled",
                    idempotency_key=f"schedule:{schedule_id}:enabled:{enabled}",
                ),
                expected_version=expected_version,
            )
            return True
        except Exception:
            return False


class ScheduledFireRepository:
    """ScheduledFire 操作投影 —— 从 schedule.fired Event 重建 / 写入临时投影。

    作为操作投影（非真相源），在 cutover 阶段可保留；生产路径保证
    schedule.fired Event 始终先于投影写入。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._event_store = EventStore(conn)

    def find(self, schedule_id: str, scheduled_fire_at: datetime) -> ScheduledFire | None:
        """从 Event stream 检查 schedule.fired 的幂等性。"""
        stream = self._event_store.read_stream(_SCHEDULE_STREAM_TYPE, schedule_id)
        projection = replay_schedule(stream, schedule_id)
        if projection is None:
            return None
        # A schedule.fired event at this time means a fire was recorded
        target_ms = epoch_ms(scheduled_fire_at)
        for event in stream:
            if event.event_type == "schedule.fired":
                fire_ms = event.attributes.get("last_fire_at")
                if fire_ms == target_ms:
                    return ScheduledFire(
                        fire_id=f"{schedule_id}:{target_ms}",
                        schedule_id=schedule_id,
                        scheduled_fire_at=scheduled_fire_at,
                        status=FireStatus.fired,
                        created_at=from_epoch_ms(event.occurred_at) if event.occurred_at else None,
                    )
        return None

    def insert(self, fire: ScheduledFire) -> None:
        """写入临时投影 —— schedule.fired Event 已在调用前写入。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO scheduled_fires "
            "(fire_id, schedule_id, scheduled_fire_at, status, task_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                fire.fire_id,
                fire.schedule_id,
                epoch_ms(fire.scheduled_fire_at),
                fire.status.value,
                fire.task_id,
                epoch_ms(fire.created_at),
            ),
        )

    def update_status(
        self,
        fire_id: str,
        status: FireStatus,
        task_id: str | None = None,
    ) -> None:
        if task_id is not None:
            self._conn.execute(
                "UPDATE scheduled_fires SET status=?, task_id=? WHERE fire_id=?",
                (status.value, task_id, fire_id),
            )
        else:
            self._conn.execute(
                "UPDATE scheduled_fires SET status=? WHERE fire_id=?",
                (status.value, fire_id),
            )
