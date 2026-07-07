"""Query Plan — 构建检索查询计划。

根据用户输入和会话上下文，生成优化后的检索参数：
- query_text: 检索文本
- kinds: 限定记忆种类
- scope: 目标 scope
- time_range_days: 时间范围

MVP 第一版使用规则推导，后续可集成轻量模型语义改写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 关键词 → 记忆种类映射
_KIND_KEYWORDS: dict[str, list[str]] = {
    "preference": ["喜欢", "偏好", "prefer", "like", "喜欢", "习惯", "风格", "style"],
    "constraint": ["不要", "禁止", "不能", "never", "don't", "禁止", "不允许"],
    "goal": ["目标", "打算", "计划", "goal", "plan", "想要", "要完成"],
    "episode": ["之前", "上次", "以前", "那时", "曾经", "过去", "before", "previous"],
}

# 时间范围关键词
_TIME_KEYWORDS: dict[str, int] = {
    "最近": 7, "昨天": 1, "今天": 1, "本周": 7, "本月": 30,
    "recent": 7, "yesterday": 1, "today": 1, "this week": 7, "this month": 30,
}


@dataclass
class QueryPlan:
    """检索查询计划。"""
    query_text: str = ""
    kinds: list[str] = field(default_factory=list)
    scope_type: str = ""
    scope_id: str = ""
    time_range_days: int = 0  # 0 = 不限


def build_query_plan(
    query: str,
    session_messages: list[dict[str, Any]] | None = None,
) -> QueryPlan:
    """根据用户输入和会话上下文构建查询计划。

    Args:
        query: 用户输入的检索文本。
        session_messages: 可选的最近会话消息用于上下文增强。

    Returns:
        优化后的查询计划。
    """
    if not query:
        return QueryPlan()

    plan = QueryPlan(query_text=query)

    # 1. 按关键词推测记忆种类
    matched_kinds: set[str] = set()
    for kind, keywords in _KIND_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in query.lower():
                matched_kinds.add(kind)
                break
    if matched_kinds:
        plan.kinds = list(matched_kinds)

    # 2. 按关键词推测时间范围
    for kw, days in _TIME_KEYWORDS.items():
        if kw in query:
            plan.time_range_days = days
            break

    # 3. 提取引号内容作为精确匹配信号（保留 query_text 不变）
    quoted = re.findall(r'"([^"]+)"', query)
    if quoted:
        # 引号内容是精确短语，query_text 保持原样（FTS5 自行处理短语）
        pass

    return plan
