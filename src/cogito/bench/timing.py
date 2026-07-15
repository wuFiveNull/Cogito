"""TurnTimer —— 单轮 Turn 分段计时器。

在所有关键路径调用 ``checkpoint(name)`` 打点，
最后一次 Turn 的完整分段记录保存在模块级 ``_last`` 中，
通过 ``/api/bench/last`` 端点读取。

计时使用 time.perf_counter_ns()，纳秒级高精度。
零依赖，仅在调用 checkpoint 时产生一次 dict 写入开销。
"""

from __future__ import annotations

import time
from typing import Any

# 最后一次 Turn 的分段记录（由各 checkpoint 累积填充）
_last: dict[str, Any] | None = None

# 当前活跃 Turn 的计时上下文
_active: dict[str, Any] | None = None


def reset(turn_id: str) -> None:
    """开启新的一轮计时（worker 领取 Turn 时调用）。"""
    global _active, _last
    _active = {
        "turn_id": turn_id,
        "checkpoints": [],
        "wall_ns": time.perf_counter_ns(),
    }


def checkpoint(name: str, *, extra: dict[str, Any] | None = None) -> None:
    """记录一个计时检查点。

    name: 段落名称（可重复调用，按出现顺序累计）。
    extra: 附带信息，如 {"delta_chars": 5, "operation_seq": 3}。
    """
    global _active
    if _active is None:
        return
    ts = time.perf_counter_ns()
    elapsed_ms = (ts - _active["wall_ns"]) / 1_000_000
    entry: dict[str, Any] = {"name": name, "offset_ms": round(elapsed_ms, 3)}
    if extra:
        entry["extra"] = extra
    _active["checkpoints"].append(entry)


def finalize() -> None:
    """结束计时，把结果存入 _last。"""
    global _active, _last
    if _active is None:
        return
    _active["total_ms"] = round((time.perf_counter_ns() - _active["wall_ns"]) / 1_000_000, 3)
    # 计算相邻 checkpoint 之间的段落耗时
    cps = _active["checkpoints"]
    for i in range(1, len(cps)):
        cps[i]["segment_ms"] = round(cps[i]["offset_ms"] - cps[i - 1]["offset_ms"], 3)
    if cps:
        cps[0]["segment_ms"] = cps[0]["offset_ms"]
    _last = _active
    _active = None


def get_last() -> dict[str, Any] | None:
    """返回最后一次 Turn 的分段记录（深拷贝不可变引用）。"""
    return _last


def is_active() -> bool:
    return _active is not None
