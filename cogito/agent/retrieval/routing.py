# cogito/agent/retrieval/routing.py
#
# RetrievalRoutingPolicy — builds a RetrievalPlan from config.
#
# This is a pure component with no I/O.  It determines which sources
# to query, with what limits and timeouts, based on the configuration
# and the current query.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from cogito.agent.domain.retrieval import (
    RetrievalPlan,
    RetrievalQuery,
    RetrievalRoute,
)

_TEXT_DEPENDENT_SOURCES = frozenset({"keyword", "vector"})


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Configuration for one retrieval source."""

    enabled: bool = True
    limit: int = 20
    timeout_seconds: float = 1.5
    weight: float = 1.0
    required: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalPhaseConfig:
    """Configuration for the InformationRetrievalPhase.

    Attributes:
        phase_timeout_seconds: Total timeout for the entire phase.
        max_concurrency: Maximum concurrent retrieval sources.
        final_limit: Maximum items in the final result list.
        rerank_candidate_limit: Items fed to the reranker.
        reranker_timeout_seconds: Timeout for the reranker call.
        reranker_fail_open: If True, reranker failure uses fusion results.
        rrf_k: K parameter for Reciprocal Rank Fusion.
        max_content_chars: Maximum characters per item content.
        max_per_kind: Maximum items per RetrievedItemKind.
        max_per_source: Maximum items per source.
        empty_query_allowed: Allow retrieval without query text.
        sources: Per-source configuration keyed by source name.
    """

    phase_timeout_seconds: float = 3.0
    max_concurrency: int = 5
    final_limit: int = 20
    rerank_candidate_limit: int = 60
    reranker_timeout_seconds: float = 1.5
    reranker_fail_open: bool = True
    rrf_k: int = 60
    max_content_chars: int = 20_000
    max_per_kind: int = 8
    max_per_source: int = 10
    empty_query_allowed: bool = True
    sources: Mapping[str, SourceConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.final_limit < 1:
            raise ValueError("final_limit must be >= 1")
        if self.rerank_candidate_limit < self.final_limit:
            raise ValueError("rerank_candidate_limit must be >= final_limit")
        if self.max_per_kind < 1:
            raise ValueError("max_per_kind must be >= 1")
        if self.max_per_source < 1:
            raise ValueError("max_per_source must be >= 1")


class RetrievalRoutingPolicy:
    """Creates a RetrievalPlan based on config and query.

    Sources whose ``enabled`` is False, or that depend on text when
    the query is empty, are excluded from the plan.
    """

    def __init__(self, config: RetrievalPhaseConfig) -> None:
        self._config = config

    def create_plan(self, query: RetrievalQuery) -> RetrievalPlan:
        """Build a RetrievalPlan from the query and config.

        Args:
            query: The retrieval query (already built by QueryBuilder).

        Returns:
            A RetrievalPlan containing only enabled, applicable routes.
        """
        routes: list[RetrievalRoute] = []
        has_text = bool(query.text.strip())

        for source_name, source_config in self._config.sources.items():
            if not source_config.enabled:
                continue

            # Skip text-dependent sources when query is empty
            if not has_text and source_name in _TEXT_DEPENDENT_SOURCES:
                if not self._config.empty_query_allowed:
                    continue

            routes.append(
                RetrievalRoute(
                    source=source_name,
                    limit=source_config.limit,
                    timeout_seconds=source_config.timeout_seconds,
                    weight=source_config.weight,
                    required=source_config.required,
                )
            )

        # Stable ordering by source name
        routes.sort(key=lambda r: r.source)

        return RetrievalPlan(query=query, routes=tuple(routes))
