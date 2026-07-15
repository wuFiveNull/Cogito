"""MemoryWeightPolicy + compute_retrieval_weight 纯函数（infra 层）。

PLAN-13 P13-05: 检索权重可解释、可重算、可回放。
指数衰减公式（MEMORY-LIFECYCLE §4 权威版本）。

放在 store 层（无 service 依赖），供 MemoryRepository（store）和
service/memory_weight.py 共同使用，避免循环导入。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

# 各 kind 默认衰减速率（/天），半衰期 = ln(2)/rate
DEFAULT_KIND_DECAY: dict[str, float] = {
    "fact": 0.001,  # ~693 天
    "preference": 0.005,  # ~139 天
    "episode": 0.02,  # ~35 天
    "goal": 0.001,
    "constraint": 0.0,  # 不自动衰减
}

# source_trust 由 explicitness 映射（MEMORY-LIFECYCLE §4.2）
EXPLICITNESS_TRUST: dict[str, float] = {
    "explicit_user_statement": 1.0,
    "confirmed_inference": 0.9,
    "external_source": 0.7,
    "system_generated": 0.6,
    "model_inference": 0.4,
}


@dataclass
class MemoryWeightPolicy:
    """版本化权重策略（PLAN-13 P13-05）。"""

    version: str = "2"
    kind_decay: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_KIND_DECAY))
    archive_threshold: float = 0.1
    forget_threshold: float = 0.05
    reinforcement_bonus_cap: float = 0.4
    emotional_bonus_cap: float = 0.2
    weight_min: float = 0.0
    weight_max: float = 2.0
    confirmation_scores: dict[str, float] = field(
        default_factory=lambda: {
            "confirmed": 1.0,
            "candidate": 0.5,
            "rejected": 0.0,
            "expired": 0.0,
        }
    )

    def kind_decay_rate(self, kind: str) -> float:
        return self.kind_decay.get(kind, 0.001)


def _source_trust(explicitness: str) -> float:
    return EXPLICITNESS_TRUST.get(explicitness, 0.5)


def _confirmation_score(status: str, policy: MemoryWeightPolicy) -> float:
    return policy.confirmation_scores.get(status, 0.0)


def compute_retrieval_weight(
    importance: float,
    source_trust: float,
    confirmation_score: float,
    kind_decay_rate: float,
    days_since_last_active: float,
    reinforcement: int,
    emotional_weight: float,
) -> float:
    """计算检索权重（MEMORY-LIFECYCLE §4 权威公式，纯函数）。"""
    importance = max(0.0, min(1.0, importance))
    source_trust = max(0.0, min(1.0, source_trust))
    confirmation_score = max(0.0, min(1.0, confirmation_score))
    days_since_last_active = max(0.0, days_since_last_active)
    reinforcement = max(0, reinforcement)
    emotional_weight = max(0.0, min(1.0, emotional_weight))

    base_score = importance * 0.6 + source_trust * 0.2 + confirmation_score * 0.2
    decay_factor = math.exp(-kind_decay_rate * days_since_last_active)
    reinforcement_bonus = min(0.4, math.log(1 + reinforcement) * 0.1)
    emotional_bonus = min(0.2, emotional_weight * 0.02)

    weight = base_score * decay_factor + reinforcement_bonus + emotional_bonus
    return max(0.0, min(2.0, weight))


def compute_weight_for_item(
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
) -> float:
    """基于 MemoryItem 字段计算 retrieval_weight 的便捷封装。"""
    days = 0.0
    if last_active_at is not None:
        delta = (now - last_active_at).total_seconds()
        days = max(0.0, delta / 86400.0)

    return compute_retrieval_weight(
        importance=importance,
        source_trust=_source_trust(explicitness),
        confirmation_score=_confirmation_score(status, policy),
        kind_decay_rate=policy.kind_decay_rate(kind),
        days_since_last_active=days,
        reinforcement=reinforcement,
        emotional_weight=emotional_weight,
    )


def weight_status(
    retrieval_weight: float,
    policy: MemoryWeightPolicy,
) -> str:
    """由 retrieval_weight 推导检索状态。"""
    if retrieval_weight < policy.forget_threshold:
        return "forgetting_candidate"
    if retrieval_weight < policy.archive_threshold:
        return "archived"
    return "searchable"
