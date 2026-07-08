"""能量模型 —— PROACTIVE-IDLE / 3. 能量模型。

公式：
  E(t) = Σ wᵢ · exp(-t / τᵢ)

t = (now - last_user_at) 分钟，τ 为半衰期参数。

首期取三档惯性 (30min, 240min, 2880min) 权重 (0.50, 0.35, 0.15)。
能量只调整 urgency 权重和 novelty/relevance 阈值 —— 不直接决定 alert。
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

DEFAULT_HALF_LIFE_MIN = (30.0, 240.0, 2880.0)
DEFAULT_WEIGHTS = (0.50, 0.35, 0.15)


def compute_energy(
    last_user_at: datetime | None,
    now: datetime | None = None,
    *,
    half_life_min: tuple[float, ...] = DEFAULT_HALF_LIFE_MIN,
    weights: tuple[float, ...] = DEFAULT_WEIGHTS,
) -> float:
    """计算当前能量 0..1。last_user_at=None → 能量=0 (从未互动)。"""
    if last_user_at is None:
        return 0.0
    if now is None:
        now = datetime.now(UTC)
    if last_user_at.tzinfo is None:
        last_user_at = last_user_at.replace(tzinfo=UTC)
    t = max(0.0, (now - last_user_at).total_seconds() / 60.0)
    energy = 0.0
    hl = half_life_min
    ws = weights
    n = min(len(hl), len(ws))
    for i in range(n):
        energy += ws[i] * math.exp(-t / hl[i])
    return min(1.0, max(0.0, energy))


def energy_band(energy: float) -> str:
    """能量分档（用于 tick 参数调整）。"""
    if energy >= 0.7:
        return "high"
    if energy >= 0.3:
        return "medium"
    return "low"
