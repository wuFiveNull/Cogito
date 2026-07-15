"""ProactiveCadencePolicy —— energy band → 下一次评估间隔 (PROACTIVE-IDLE / 3)。

纯函数、确定性：给定 (energy_band, cadence_config) + 可注入 RNG，返回下一次
评估间隔（秒），带 jitter 与上下限裁剪。

设计要点：
- 高能量 (用户最近活跃) → 短间隔、更频繁评估（用户更可能响应）。
- 低能量 → 拉长间隔，避免打扰与资源浪费。
- jitter 打破同步共振；RNG 可注入，测试可复现。
- 区间严格限制在 [min_interval_seconds, max_interval_seconds]。
"""

from __future__ import annotations

import random

from cogito.config import ProactiveCadenceConfig


def compute_interval(
    energy_band: str,
    cadence: ProactiveCadenceConfig,
    rng: random.Random | None = None,
) -> int:
    """根据能量档计算下一次评估间隔（秒）。

    Args:
        energy_band: "high" | "medium" | "low"
        cadence: 节拍配置
        rng: 可注入随机数发生器；None 使用全局 random
    Returns:
        带 jitter 并裁剪到 [min, max] 的整数秒
    """
    if rng is None:
        rng = random

    base = _base_interval(energy_band, cadence)
    # 上下限裁剪
    clamped = max(cadence.min_interval_seconds, min(cadence.max_interval_seconds, base))
    # jitter: ± jitter_ratio
    jitter = int(clamped * cadence.jitter_ratio)
    if jitter > 0:
        delta = rng.randint(-jitter, jitter)
        clamped = max(
            cadence.min_interval_seconds, min(cadence.max_interval_seconds, clamped + delta)
        )
    return int(clamped)


def _base_interval(energy_band: str, cadence: ProactiveCadenceConfig) -> int:
    band = energy_band or "medium"
    if band == "high":
        return cadence.high_energy_interval_seconds
    if band == "low":
        return cadence.low_energy_interval_seconds
    return cadence.medium_energy_interval_seconds
