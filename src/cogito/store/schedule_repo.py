"""Schedule / ScheduledFire 数据访问层。

持久化调度配置和触发记录（幂等键 schedule_id + scheduled_fire_at）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from cogito.contracts.clock import epoch_ms, from_epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.schedule import (
    FireStatus,
    Schedule,
    ScheduledFire,
)
from cogito.store.event_store import EventStore


class ScheduleRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, schedule_id: str) -> Schedule | None:
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE schedule_id=?",
            (schedule_id,),
        ).fetchone()
        return self._row_to_schedule(row) if row else None

    def insert(self, schedule: Schedule) -> None:
        normalized_s = self._compute_interval(schedule.expression)
        EventStore(self._conn).append(
            Event(
                event_type="schedule.created",
                stream_type="schedule",
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
        # Legacy table INSERT for FK compatibility
        self._conn.execute(
            "INSERT INTO schedules (schedule_id, schedule_type, expression, "
            "  timezone, misfire_policy, max_catch_up, enabled, "
            "  next_fire_at, last_fire_at, normalized_interval_s, "
            "  version, connector_id, created_at) "
            "VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?)",
            (
                schedule.schedule_id,
                schedule.schedule_type.value,
                schedule.expression,
                schedule.timezone,
                schedule.misfire_policy.value,
                schedule.max_catch_up,
                1 if schedule.enabled else 0,
                epoch_ms(schedule.next_fire_at),
                epoch_ms(schedule.last_fire_at),
                normalized_s,
                schedule.version,
                schedule.connector_id,
                epoch_ms(schedule.created_at),
            ),
        )

    @staticmethod
    def _compute_interval(expression: str) -> int | None:
        """从 expression 估算触发间隔（秒）。"""
        from cogito.domain.schedule import parse_duration

        delta = parse_duration(expression.strip())
        if delta is not None:
            return int(delta.total_seconds())
        return None

    def find_due(self, now: datetime, limit: int = 10) -> list[Schedule]:
        """查找已到期的 enabled schedules。"""
        now_ms = epoch_ms(now)
        rows = self._conn.execute(
            "SELECT * FROM schedules "
            "WHERE enabled = 1 AND next_fire_at IS NOT NULL AND next_fire_at <= ? "
            "ORDER BY next_fire_at ASC LIMIT ?",
            (now_ms, limit),
        ).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def find_all(self, limit: int = 100) -> list[Schedule]:
        rows = self._conn.execute(
            "SELECT * FROM schedules ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def update_fire_time(
        self,
        schedule_id: str,
        next_fire_at: datetime | None,
        last_fire_at: datetime | None,
        expected_version: int,
    ) -> bool:
        """条件更新触发时间（乐观锁）。返回 True 表示更新成功。"""
        EventStore(self._conn).append(
            Event(
                event_type="schedule.fired",
                stream_type="schedule",
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
                idempotency_key=f"schedule:{schedule_id}:fired:{epoch_ms(next_fire_at) if next_fire_at else 'none'}",
            ),
        )
        cursor = self._conn.execute(
            "UPDATE schedules SET next_fire_at=?, last_fire_at=?, version=version+1 "
            "WHERE schedule_id=? AND version=?",
            (
                epoch_ms(next_fire_at),
                epoch_ms(last_fire_at),
                schedule_id,
                expected_version,
            ),
        )
        return cursor.rowcount > 0

    def update_enabled(self, schedule_id: str, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE schedules SET enabled=? WHERE schedule_id=?",
            (1 if enabled else 0, schedule_id),
        )

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> Schedule:
        return Schedule(
            schedule_id=row["schedule_id"],
            schedule_type=row["schedule_type"],
            expression=row["expression"],
            timezone=row["timezone"],
            misfire_policy=row["misfire_policy"],
            max_catch_up=row["max_catch_up"],
            enabled=bool(row["enabled"]),
            next_fire_at=from_epoch_ms(row["next_fire_at"]),
            last_fire_at=from_epoch_ms(row["last_fire_at"]),
            version=row["version"],
            connector_id=row["connector_id"],
            created_at=from_epoch_ms(row["created_at"]),
            task_type=(row["task_type"] if "task_type" in row.keys() else "connector.poll"),
            task_payload=(row["task_payload"] if "task_payload" in row.keys() else ""),
        )

    def update_enabled_expected(
        self,
        schedule_id: str,
        enabled: bool,
        expected_version: int,
    ) -> bool:
        """Update enabled atomically under the caller's observed version."""
        cursor = self._conn.execute(
            "UPDATE schedules SET enabled=?, version=version+1 WHERE schedule_id=? AND version=?",
            (1 if enabled else 0, schedule_id, expected_version),
        )
        return cursor.rowcount > 0


class ScheduledFireRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, schedule_id: str, scheduled_fire_at: datetime) -> ScheduledFire | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_fires WHERE schedule_id=? AND scheduled_fire_at=?",
            (schedule_id, epoch_ms(scheduled_fire_at)),
        ).fetchone()
        return self._row_to_fire(row) if row else None

    def insert(self, fire: ScheduledFire) -> None:
        self._conn.execute(
            "INSERT INTO scheduled_fires "
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

    @staticmethod
    def _row_to_fire(row: sqlite3.Row) -> ScheduledFire:
        return ScheduledFire(
            fire_id=row["fire_id"],
            schedule_id=row["schedule_id"],
            scheduled_fire_at=from_epoch_ms(row["scheduled_fire_at"]),
            status=row["status"],
            task_id=row["task_id"],
            created_at=from_epoch_ms(row["created_at"]),
        )
