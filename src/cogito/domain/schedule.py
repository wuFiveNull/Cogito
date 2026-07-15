"""Schedule 领域实体 + 调度表达式解析器。

TASK-SCHEDULER / 7. Schedule 对象、调度表达式、Misfire 策略。
PROACTIVE-TASKS / 4.4-4.5 调度表达式与自然语言格式。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo


class ScheduleStatus(StrEnum):
    active = "active"
    paused = "paused"
    disabled = "disabled"


class ScheduleType(StrEnum):
    once = "once"
    interval = "interval"
    cron = "cron"


class MisfirePolicy(StrEnum):
    skip = "skip"
    run_once = "run_once"
    catch_up_limited = "catch_up_limited"
    merge = "merge"


class FireStatus(StrEnum):
    pending = "pending"
    fired = "fired"
    skipped = "skipped"


class Schedule:
    """持久化的调度配置。"""

    def __init__(
        self,
        schedule_id: str | None = None,
        schedule_type: ScheduleType = ScheduleType.interval,
        expression: str = "30m",
        timezone: str = "UTC",
        misfire_policy: MisfirePolicy = MisfirePolicy.catch_up_limited,
        max_catch_up: int = 3,
        enabled: bool = True,
        next_fire_at: datetime | None = None,
        last_fire_at: datetime | None = None,
        version: int = 1,
        connector_id: str | None = None,
        created_at: datetime | None = None,
        dst_policy: str = "post",
        task_type: str = "connector.poll",
        task_payload: str = "",
    ) -> None:
        self.schedule_id = schedule_id or uuid.uuid4().hex
        self.schedule_type = ScheduleType(schedule_type)
        self.expression = expression
        self.timezone = timezone
        self.misfire_policy = MisfirePolicy(misfire_policy)
        self.max_catch_up = max_catch_up
        self.enabled = enabled
        self.next_fire_at = next_fire_at
        self.last_fire_at = last_fire_at
        self.version = version
        self.connector_id = connector_id
        self.created_at = created_at or datetime.now(UTC)
        self.dst_policy = dst_policy
        self.task_type = task_type
        self.task_payload = task_payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "schedule_type": self.schedule_type.value,
            "expression": self.expression,
            "timezone": self.timezone,
            "misfire_policy": self.misfire_policy.value,
            "max_catch_up": self.max_catch_up,
            "enabled": self.enabled,
            "next_fire_at": self.next_fire_at.isoformat() if self.next_fire_at else None,
            "last_fire_at": self.last_fire_at.isoformat() if self.last_fire_at else None,
            "version": self.version,
            "connector_id": self.connector_id,
            "created_at": self.created_at.isoformat(),
            "task_type": self.task_type,
            "task_payload": self.task_payload,
        }

    def __repr__(self) -> str:
        return (
            f"Schedule({self.schedule_id}, {self.schedule_type.value}, "
            f"{self.expression}, next={self.next_fire_at})"
        )


class ScheduledFire:
    """单次触发记录 —— 幂等键 schedule_id + scheduled_fire_at。"""

    def __init__(
        self,
        fire_id: str | None = None,
        schedule_id: str = "",
        scheduled_fire_at: datetime | None = None,
        status: FireStatus = FireStatus.pending,
        task_id: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.fire_id = fire_id or uuid.uuid4().hex
        self.schedule_id = schedule_id
        self.scheduled_fire_at = scheduled_fire_at
        self.status = FireStatus(status)
        self.task_id = task_id
        self.created_at = created_at or datetime.now(UTC)

    def __repr__(self) -> str:
        return (
            f"ScheduledFire({self.fire_id}, schedule={self.schedule_id}, "
            f"at={self.scheduled_fire_at}, {self.status.value})"
        )


# ── 调度表达式解析 ──

# Duration 格式: (Nd)?(Nh)?(Nm)?(Ns)?
_DURATION_RE = re.compile(
    r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$"
)

# "every" 短语
_EVERY_DURATION_RE = re.compile(
    r"^every\s+(\d+)\s*([smh])$"
)
_EVERY_WEEKLY_RE = re.compile(
    r"^every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)
_EVERY_DAILY_RE = re.compile(
    r"^every\s+day\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)
_EVERY_DAY_RE = re.compile(r"^every\s+1d$")

_WEEKDAY_MAP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def parse_duration(expr: str) -> timedelta | None:
    """解析 Duration 格式 '30s'/'5m'/'2h'/'1d'/'1h30m'。"""
    m = _DURATION_RE.match(expr.strip())
    if not m:
        return None
    days, hours, minutes, seconds = m.groups()
    if not any((days, hours, minutes, seconds)):
        return None
    total = timedelta(
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=int(seconds or 0),
    )
    if total.total_seconds() < 30:
        return None  # 最小 30s
    return total


def _parse_time(hour_str: str, minute_str: str | None, ampm: str | None) -> tuple[int, int]:
    """解析时间为 (hour, minute)。"""
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    return hour, minute


def _next_weekday(after: datetime, weekday: int, hour: int, minute: int) -> datetime:
    """计算 after 之后的下一个指定星期几+时间。"""
    days_ahead = weekday - after.weekday()
    if days_ahead < 0 or (days_ahead == 0 and (after.hour, after.minute) >= (hour, minute)):
        days_ahead += 7
    target = after.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    return target


# ── DST 确定策略 ──
# spring-forward (gap): 跳到 gap 之后
# fall-back (overlap): fold=0 选择较早（UTC 较晚）的那个
DST_POLICY = "post"       # gap → 跳到 gap 之后
FOLD_POLICY = "earlier"   # overlap → fold=0


def _localize_with_dst(
    dt: datetime,
    tz: ZoneInfo,
    policy: str = DST_POLICY,
) -> datetime:
    """本地化 datetime，处理 DST gap/overlap（fold 确定策略）。"""
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    # fold=0 表示较早的本地时间（fall-back 时 UTC 较晚）
    localized = dt.replace(tzinfo=tz, fold=0)
    # 检测 gap (spring forward): 如果 naive 时间不存在于 tz
    # 表现为：localized 比预期偏移了 1 小时
    if policy == "post":
        # 跳到 gap 之后：如果 fold=0 的时间被解释为 gap 后，加 1 小时
        pre = (dt - timedelta(minutes=1)).replace(tzinfo=tz, fold=0)
        post = (dt + timedelta(minutes=1)).replace(tzinfo=tz, fold=0)
        if (post - pre) > timedelta(hours=1, minutes=2):
            # 处于 gap 内，跳到 gap 之后
            return (dt + timedelta(hours=1)).replace(tzinfo=tz, fold=0)
    return localized


def _apply_local_timezone(
    dt: datetime,
    tz: ZoneInfo,
    policy: str = DST_POLICY,
) -> datetime:
    """将 naive/UTC datetime 转为带 DST 处理的本地时间。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if str(tz) == "UTC" or not hasattr(tz, "key") or tz.key == "UTC":
        return dt
    return _localize_with_dst(dt.astimezone(UTC).replace(tzinfo=None), tz, policy)


def next_fire_at(
    expression: str,
    timezone: str = "UTC",
    after: datetime | None = None,
    dst_policy: str = DST_POLICY,
) -> datetime | None:
    """计算下次触发时间。

    解析顺序: ISO 时间戳 → "every"短语 → Duration → cron 表达式。
    支持 DST 确定策略（gap/overlap 处理）。

    Args:
        dst_policy: "post" (gap 跳到之后) 或 "pre" (gap 跳到之前)。
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone) if timezone and timezone != "UTC" else UTC
    after = after or datetime.now(tz)
    is_non_utc = str(tz) != "UTC" and hasattr(tz, "key") and tz.key != "UTC"

    expr = expression.strip()

    # 1. ISO 时间戳 → once
    try:
        dt = datetime.fromisoformat(expr.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        pass

    # 2. "every" 短语
    # "every 1d"
    if _EVERY_DAY_RE.match(expr):
        return after + timedelta(days=1)

    # "every 2h" / "every 30m"
    m = _EVERY_DURATION_RE.match(expr)
    if m:
        value, unit = int(m.group(1)), m.group(2)
        delta = timedelta(**{"hours" if unit == "h" else "minutes" if unit == "m" else "seconds": value})
        return after + delta

    # "every monday 9am"
    m = _EVERY_WEEKLY_RE.match(expr)
    if m:
        weekday = _WEEKDAY_MAP[m.group(1).lower()]
        hour, minute = _parse_time(m.group(2), m.group(3), m.group(4))
        result = _next_weekday(after, weekday, hour, minute)
        if is_non_utc:
            result = _apply_local_timezone(result, tz, dst_policy)
        return result

    # "every day 08:00"
    m = _EVERY_DAILY_RE.match(expr)
    if m:
        hour, minute = _parse_time(m.group(1), m.group(2), m.group(3))
        target = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= after:
            target += timedelta(days=1)
        if is_non_utc:
            target = _apply_local_timezone(target, tz, dst_policy)
        return target

    # 3. Duration 格式
    delta = parse_duration(expr)
    if delta is not None:
        return after + delta

    # 4. 5-field cron
    cron_next = _cron_next_fire(expr, after)
    if cron_next is not None:
        if is_non_utc:
            cron_next = _apply_local_timezone(cron_next, tz, dst_policy)
        return cron_next

    return None


# ── 轻量 cron 解析器 ──

_CRON_FIELD_RE = re.compile(r"^(\*|\d+|\*/\d+|\d+-\d+(/\d+)?)(,(\*|\d+|\*/\d+|\d+-\d+(/\d+)?))*$")


def _parse_cron_field(field: str, min_val: int, max_val: int) -> set[int] | None:
    """解析单个 cron 字段，返回匹配值集合。"""
    values: set[int] = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(min_val, max_val + 1))
            continue
        step_match = re.match(r"^\*/(\d+)$", part)
        if step_match:
            step = int(step_match.group(1))
            values.update(range(min_val, max_val + 1, step))
            continue
        range_match = re.match(r"^(\d+)-(\d+)(?:/(\d+))?$", part)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            step = int(range_match.group(3)) if range_match.group(3) else 1
            values.update(range(start, end + 1, step))
            continue
        if part.isdigit():
            values.add(int(part))
            continue
        return None  # 无法解析
    return values


def _cron_next_fire(expression: str, after: datetime) -> datetime | None:
    """计算 cron 表达式的下次触发时间。

    支持 5-field cron: 分 时 日 月 周。
    向前搜索最多 366 天。
    """
    parts = expression.split()
    if len(parts) != 5:
        return None
    minute_set = _parse_cron_field(parts[0], 0, 59)
    hour_set = _parse_cron_field(parts[1], 0, 23)
    dom_set = _parse_cron_field(parts[2], 1, 31)
    month_set = _parse_cron_field(parts[3], 1, 12)
    dow_set = _parse_cron_field(parts[4], 0, 6)

    if any(s is None for s in (minute_set, hour_set, dom_set, month_set, dow_set)):
        return None

    # 从 after 的下一分钟开始搜索
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    for _ in range(366 * 24 * 60):  # 最多 366 天
        if (
            candidate.minute in minute_set
            and candidate.hour in hour_set
            and candidate.day in dom_set
            and candidate.month in month_set
            and candidate.weekday() in dow_set
        ):
            return candidate
        candidate += timedelta(minutes=1)

    return None
