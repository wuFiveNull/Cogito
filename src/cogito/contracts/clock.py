"""Clock + epoch-ms time contract (PLAN-09 M2).

Shared by runtime / store / model / capability / service / interaction_web.
This module is part of the `cogito.contracts` pure layer: it must NOT
import any infrastructure subpackage (store, model, runtime, service, ...).

Two pieces live here:
  - `Clock` Protocol + `ProductionClock` + `FakeClock`  (moved from runtime.clock)
  - `epoch_ms` / `from_epoch_ms` / `now_ms` / `iso_to_epoch_ms`
    (moved from store.time_utils)

Legacy import paths are preserved as re-exports for one migration cycle:
  - `cogito.runtime.clock`  → re-exports from here
  - `cogito.store.time_utils` → re-exports from here

Once every caller has been migrated to this module, the re-export wrappers
can be removed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

# ── Clock ────────────────────────────────────────────────────────────────


class Clock(Protocol):
    """时间源协议 —— 提供当前 UTC 时间。

    所有依赖当前时间的服务使用统一的 Clock 接口，
    允许测试在不使用 sleep 的前提下精确控制时间推进。
    """

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


# ── epoch-ms conversion ──────────────────────────────────────────────────


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
            return datetime.fromisoformat(ms)
        except ValueError:
            pass
        try:
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
