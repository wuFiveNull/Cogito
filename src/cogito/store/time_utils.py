"""Time utilities — epoch ms conversion for SQLite storage.

DATABASE-SCHEMA / 1. SQLite 模式：
数据库时间统一保存为 UTC epoch milliseconds（INTEGER）。
Python 边界使用带时区 datetime。
跨进程 Contract 使用 RFC 3339 字符串。

from_epoch_ms 兼容处理 TEXT（ISO 8601）和 INTEGER（epoch ms）两种格式，
支持从旧 TEXT 列和新 INTEGER 列的安全读取。
"""

from __future__ import annotations

from datetime import UTC, datetime

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_ms(dt: datetime | None = None) -> int | None:
    """将带时区 datetime 转换为 UTC epoch milliseconds。

    若 dt 为 None，返回 None。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int | str | None) -> datetime | None:
    """将 UTC epoch milliseconds 或 ISO 时间字符串转换为带时区 datetime。

    接受两种格式：
    - INTEGER: epoch ms（新格式）
    - TEXT: ISO 8601 字符串（旧格式兼容）

    若 ms 为 None，返回 None。
    若无法解析，返回 None 而非抛出异常。
    """
    if ms is None:
        return None
    if isinstance(ms, (int, float)):
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    if isinstance(ms, str) and ms.strip():
        try:
            # 尝试 ISO 格式
            return datetime.fromisoformat(ms)
        except ValueError:
            pass
        try:
            # 尝试整数格式的字符串
            return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
        except (ValueError, OverflowError):
            pass
    return None


def now_ms() -> int:
    """当前 UTC 时间的 epoch milliseconds。"""
    return epoch_ms(datetime.now(UTC))  # type: ignore[return-value]


def iso_to_epoch_ms(iso_str: str | None) -> int | None:
    """将 RFC 3339 字符串转换为 epoch milliseconds。"""
    if iso_str is None:
        return None
    dt = datetime.fromisoformat(iso_str)
    return epoch_ms(dt)
