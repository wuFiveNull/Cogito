"""启发式相关性评分 + 决策。

PROACTIVE-IDLE / 5. 决策顺序、/ 7. Quiet Hours 与冷却。

公式: score = 0.6 * keyword_match + 0.4 * recency_decay
决策: score >= threshold → digest, 否则 silent
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

_LOGGER = logging.getLogger(__name__)

# 时效性半衰期（小时）
RECENCY_HALF_LIFE_H = 24.0


def score_relevance(
    title: str,
    summary: str,
    published_at: datetime | None,
    interests: list[str],
) -> float:
    """计算条目的相关性得分（0.0~1.0）。"""
    text = f"{title} {summary}".lower()

    # 关键词匹配
    keyword_score = 0.0
    if interests:
        hits = sum(1 for kw in interests if kw.lower() in text)
        keyword_score = min(1.0, hits / max(1, min(len(interests), 3)))
    else:
        # 无兴趣配置时中性分
        keyword_score = 0.3

    # 时效性衰减
    recency_score = 0.5  # 无发布时间时中性
    if published_at is not None:
        now = datetime.now(UTC)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        age_h = max(0.0, (now - published_at).total_seconds() / 3600.0)
        recency_score = math.exp(-age_h / RECENCY_HALF_LIFE_H)  # 1.0 → 0.0

    return 0.6 * keyword_score + 0.4 * recency_score


def decide(score: float, threshold: float = 0.4) -> str:
    """基于阈值决策: 'digest' 或 'silent'。"""
    return "digest" if score >= threshold else "silent"
