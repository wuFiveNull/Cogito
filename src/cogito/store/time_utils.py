"""Time utilities — epoch ms conversion for SQLite storage.

DATABASE-SCHEMA / 1. SQLite 模式：
数据库时间统一保存为 UTC epoch milliseconds（INTEGER）。
Python 边界使用带时区 datetime。
跨进程 Contract 使用 RFC 3339 字符串。
"""

from __future__ import annotations

from datetime import UTC, datetime

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_ms(dt: datetime | None = None) -> int | None:
    """将带时区 datetime 转换为 UTC epoch milliseconds。

    若 dt 为 None 或无时区信息（视为 UTC），返回 None。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int | None) -> datetime | None:
    """将 UTC epoch milliseconds 转换为带时区 datetime。

    若 ms 为 None，返回 None。
    """
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def now_ms() -> int:
    """当前 UTC 时间的 epoch milliseconds。"""
    return epoch_ms(datetime.now(UTC))  # type: ignore[return-value]


def iso_to_epoch_ms(iso_str: str | None) -> int | None:
    """将 RFC 3339 字符串转换为 epoch milliseconds。"""
    if iso_str is None:
        return None
    dt = datetime.fromisoformat(iso_str)
    return epoch_ms(dt)
