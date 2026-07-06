"""Clock interface — abstract time source for deterministic testing.

PR 8.3-A / GLOBAL-INVARIANTS / 2. 执行：
所有依赖当前时间的服务使用统一的 Clock 接口，
允许测试在不使用 sleep 的前提下精确控制时间推进。

ProductionClock 返回当前 UTC 时间。
FakeClock 支持显式 advance()，只用于测试。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """时间源协议 —— 提供当前 UTC 时间。"""

    def now(self) -> datetime:
        """返回当前 UTC 时间。"""
        ...


class ProductionClock:
    """生产环境 Clock：返回当前 UTC 时间。"""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """测试用 Clock：固定初始时间，支持 advance() 向前推进。

    禁止使用 sleep 测试 Lease 过期。应使用 FakeClock 推进时间。
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._current = start or datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float = 0, milliseconds: float = 0) -> None:
        """向前推进时间。"""
        self._current += timedelta(seconds=seconds, milliseconds=milliseconds)

    def advance_minutes(self, minutes: float = 1) -> None:
        self._current += timedelta(minutes=minutes)

    def advance_hours(self, hours: float = 1) -> None:
        self._current += timedelta(hours=hours)

    def __repr__(self) -> str:
        return f"FakeClock({self._current.isoformat()})"
