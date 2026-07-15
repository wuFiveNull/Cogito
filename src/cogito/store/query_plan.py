"""Query Plan — 轻量检索查询构建。

E1: 移除规则化语义判断。
第一版直接回退（fail-open），不做任何关键词→kinds 的推断。
可选择接入 LLM 改写器（E2），失败时直接回退到原始 query。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryRewriteRequest:
    """轻型改写请求 (PLAN-10 M1: 隔离 store 对 model.contracts.ModelRequest 的依赖)。"""

    messages: tuple[dict[str, Any], ...] = ()
    response_format: str | None = "json"


@dataclass
class QueryPlan:
    """检索查询计划。"""

    query_text: str = ""
    kinds: list[str] = field(default_factory=list)
    scope_type: str = ""
    scope_id: str = ""
    time_range_days: int = 0  # 0 = 不限
    needs_episodic: bool = False
    needs_procedure: bool = False
    original_query: str = ""


def build_query_plan(
    query: str,
    session_messages: list[dict[str, Any]] | None = None,
) -> QueryPlan:
    """构建查询计划（E1: 不做规则化语义猜测，直接保留原始 query）。

    Args:
        query: 用户输入的检索文本。
        session_messages: 可选的最近会话消息（当前未使用，保留接口）。

    Returns:
        查询计划（query_text = 原始输入）。
    """
    if not query:
        return QueryPlan()
    return QueryPlan(query_text=query.strip(), original_query=query)


async def build_query_plan_with_llm(
    query: str,
    model_router: Any = None,
    session_messages: list[dict[str, Any]] | None = None,
    timeout_ms: int = 800,
) -> QueryPlan:
    """E2: 可选 LLM 改写，超时 fail-open 回退到 build_query_plan。"""
    if not model_router:
        return build_query_plan(query, session_messages)

    try:
        import asyncio

        return await asyncio.wait_for(
            _call_rewriter(query, model_router, session_messages),
            timeout=timeout_ms / 1000.0,
        )
    except (Exception, asyncio.TimeoutError):
        return build_query_plan(query, session_messages)


async def _call_rewriter(
    query: str,
    model_router: Any,
    session_messages: list[dict[str, Any]] | None = None,
) -> QueryPlan:
    """调用轻量模型改写 query（E2, 可选）。"""
    try:
        messages = (
            {
                "role": "system",
                "content": (
                    "Rewrite the user's query for memory retrieval. "
                    'Return JSON: {"query": "rewritten query", '
                    '"kinds": ["preference"|"fact"|"goal"|"constraint"|"episode"], '
                    '"needs_episodic": bool, "needs_procedure": bool}'
                ),
            },
            {"role": "user", "content": query},
        )
        request = QueryRewriteRequest(messages=messages, response_format="json")
        response = await model_router.generate(request, model_role="query_rewriter")

        import json

        text = response.text.strip()
        json_match = __import__("re").search(r"\{.*\}", text, __import__("re").DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
            return QueryPlan(
                query_text=data.get("query", query),
                kinds=data.get("kinds", []),
                original_query=query,
                needs_episodic=bool(data.get("needs_episodic")),
                needs_procedure=bool(data.get("needs_procedure")),
            )
    except Exception as e:
        _LOGGER.debug("LLM query rewrite failed: %s", e)

    return build_query_plan(query, session_messages)
