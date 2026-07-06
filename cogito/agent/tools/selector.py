# cogito/agent/tools/selector.py
#
# HybridToolSelector — query-aware tool selection with LRU tracking.
#
# Design rules (see tool-system-spec §8.4):
#   - Always-visible tools always come first.
#   - Recently-used tools are boosted (LRU from session context).
#   - Remaining tools are ranked by query-tool relevance (keyword match).
#   - Total is capped by model_max_tools.
#   - Tools beyond the cap are marked deferred.

from __future__ import annotations

import logging
from collections import OrderedDict
from difflib import SequenceMatcher

from cogito.agent.domain.tools import ToolDefinition

logger = logging.getLogger(__name__)


class HybridToolSelector:
    """Selects tools from a candidate pool using query relevance and LRU.

    The selector has two pluggable strategies:
      - Query relevance: keyword/tf-idf matching against descriptions + tags.
      - Session LRU: recently-used tools are preferred.

    The default implementation uses simple keyword overlap scoring.
    A more sophisticated embedding-based selector can be substituted.
    """

    def __init__(
        self,
        *,
        lru_size: int = 8,
        search_top_k: int = 16,
        relevance_weight: float = 0.6,
        lru_weight: float = 0.3,
        always_visible_weight: float = 1.0,
    ) -> None:
        self._lru_size = lru_size
        self._search_top_k = search_top_k
        self._relevance_weight = relevance_weight
        self._lru_weight = lru_weight
        self._always_visible_weight = always_visible_weight

        # Session-scoped LRU: session_id → OrderedDict of tool names
        self._session_lru: dict[str, OrderedDict[str, float]] = {}

    # ── Public API ──────────────────────────────────────────────────────

    def select(
        self,
        *,
        candidates: list[ToolDefinition],
        query: str,
        model_max_tools: int,
        always_visible: frozenset[str],
    ) -> tuple[list[ToolDefinition], list[ToolDefinition], dict[str, str]]:
        """Select tools from candidates, return (selected, deferred, reasons)."""
        if model_max_tools <= 0:
            return [], candidates, {}

        reasons: dict[str, str] = {}
        scored: list[tuple[float, ToolDefinition]] = []

        for defn in candidates:
            score = 0.0

            # Always-visible gets max weight
            if defn.name in always_visible or defn.always_visible:
                score += self._always_visible_weight * 100.0
                reasons[defn.name] = "always_visible"
                scored.append((score, defn))
                continue

            # LRU boost
            lru_score = self._get_lru_score(defn.name)
            score += lru_score * self._lru_weight

            # Relevance score (keyword overlap)
            relevance = self._compute_relevance(defn, query)
            score += relevance * self._relevance_weight

            scored.append((score, defn))

        # Sort by score descending, then name for stability
        scored.sort(key=lambda x: (-x[0], x[1].name))

        selected: list[ToolDefinition] = []
        deferred: list[ToolDefinition] = []

        for i, (score, defn) in enumerate(scored):
            if i < model_max_tools:
                selected.append(defn)
                if defn.name not in reasons:
                    reasons[defn.name] = f"score_{score:.2f}"
            else:
                deferred.append(defn)
                reasons[defn.name] = f"deferred_budget"

        return selected, deferred, reasons

    # ── LRU tracking ───────────────────────────────────────────────────

    def record_usage(
        self,
        *,
        session_id: str,
        tool_name: str,
        weight: float = 1.0,
    ) -> None:
        """Record a tool usage in the session's LRU."""
        if session_id not in self._session_lru:
            self._session_lru[session_id] = OrderedDict()

        lru = self._session_lru[session_id]
        lru[tool_name] = weight
        lru.move_to_end(tool_name)

        # Trim to size
        while len(lru) > self._lru_size:
            lru.popitem(last=False)

    def _get_lru_score(self, tool_name: str) -> float:
        """Get LRU recency score where 0 = never used, 1 = most recent."""
        best: float = 0.0
        for lru in self._session_lru.values():
            if tool_name in lru:
                # Recent items (at end) get higher scores
                idx = list(lru.keys()).index(tool_name)
                score = (idx + 1) / max(len(lru), 1)
                best = max(best, score)
        return best

    # ── Relevance scoring ──────────────────────────────────────────────

    @staticmethod
    def _compute_relevance(defn: ToolDefinition, query: str) -> float:
        """Compute query-tool relevance using keyword overlap and name match."""
        if not query.strip():
            return 0.0

        query_lower = query.lower()
        score = 0.0

        # Exact name match
        if defn.name.lower() == query_lower:
            score += 0.8
        elif defn.name.lower() in query_lower or query_lower in defn.name.lower():
            score += 0.5

        # Description keyword overlap
        desc_lower = defn.description.lower()
        query_words = set(query_lower.split())

        if query_words:
            matches = sum(1 for w in query_words if w in desc_lower)
            score += (matches / len(query_words)) * 0.4

        # Tag matching
        if defn.tags:
            tag_matches = sum(1 for t in defn.tags if t.lower() in query_lower)
            if tag_matches:
                score += 0.3 * (tag_matches / max(len(defn.tags), 1))

        # Fuzzy name match for partial tool names
        matcher = SequenceMatcher(
            None,
            defn.name.lower(),
            query_lower,
        )
        score += matcher.ratio() * 0.2

        return min(score, 1.0)
