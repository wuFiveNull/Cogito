"""记忆权重策略的上层封装（service 层）。

PLAN-13 P13-05: 提供 explain_weight（Explain API）等上层功能。
基础纯函数与策略定义位于 store/weight_policy.py（避免循环导入）。
"""
from __future__ import annotations

from datetime import datetime

from cogito.store.weight_policy import (
    MemoryWeightPolicy,
    _source_trust,
    _confirmation_score,
    compute_retrieval_weight,
    compute_weight_for_item,
    weight_status,
)


__all__ = [
    "MemoryWeightPolicy",
    "compute_retrieval_weight",
    "compute_weight_for_item",
    "explain_weight",
    "weight_status",
]


def explain_weight(
    *,
    importance: float,
    explicitness: str,
    status: str,
    kind: str,
    last_active_at: datetime | None,
    now: datetime,
    reinforcement: int,
    emotional_weight: float,
    policy: MemoryWeightPolicy,
) -> dict[str, float]:
    """返回权重分项解释（用于 Explain API）。"""
    import math
    source_trust = _source_trust(explicitness)
    confirmation_score = _confirmation_score(status, policy)
    days = 0.0
    if last_active_at is not None:
        delta = (now - last_active_at).total_seconds()
        days = max(0.0, delta / 86400.0)
    kind_decay_rate = policy.kind_decay_rate(kind)
    base_score = importance * 0.6 + source_trust * 0.2 + confirmation_score * 0.2
    decay_factor = math.exp(-kind_decay_rate * days)
    reinforcement_bonus = min(0.4, math.log(1 + reinforcement) * 0.1)
    emotional_bonus = min(0.2, emotional_weight * 0.02)

    return {
        "base_score": round(base_score, 4),
        "source_trust": round(source_trust, 4),
        "confirmation_score": round(confirmation_score, 4),
        "kind_decay_rate": kind_decay_rate,
        "days_since_last_active": round(days, 2),
        "decay_factor": round(decay_factor, 6),
        "reinforcement": reinforcement,
        "reinforcement_bonus": round(reinforcement_bonus, 4),
        "emotional_bonus": round(emotional_bonus, 4),
        "retrieval_weight": round(
            max(0.0, min(2.0, base_score * decay_factor + reinforcement_bonus + emotional_bonus)), 4
        ),
        "algorithm_version": policy.version,
    }
