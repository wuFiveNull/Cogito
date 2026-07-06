# cogito/agent/retrieval/diagnostics.py
#
# RetrievalDiagnosticsBuilder — builds RetrievalDiagnostics object.
#
# Collects execution stats, source outcomes, and status into a single
# immutable diagnostic object.  No user content is included in the
# diagnostic — only counts, durations, and source names.

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from cogito.agent.domain.retrieval import (
    RetrievalCompletionStatus,
    RetrievalDiagnostics,
    RetrievalPlan,
    RetrievalSourceFailure,
    RetrievalSourceStats,
)
from cogito.agent.retrieval.routing import RetrievalPhaseConfig


@dataclass(frozen=True, slots=True)
class RetrievalDiagnosticsBuilder:
    """Builds a RetrievalDiagnostics object from execution results.

    The diagnostics object is written to TurnContext after all
    retrieval steps complete.  It contains no item content or
    user-identifiable information.
    """

    def empty(
        self,
        *,
        total_duration_ms: int,
    ) -> RetrievalDiagnostics:
        """Build diagnostics for an empty plan (no routes)."""
        return RetrievalDiagnostics(
            status=RetrievalCompletionStatus.EMPTY,
            total_duration_ms=total_duration_ms,
            selected_sources=(),
            successful_sources=(),
            source_stats=(),
            failures=(),
            pre_fusion_count=0,
            post_fusion_count=0,
            final_count=0,
            reranker_used=False,
            reranker_degraded=False,
        )

    def build(
        self,
        *,
        status: RetrievalCompletionStatus,
        total_duration_ms: int,
        plan: RetrievalPlan,
        outcomes: Sequence[_SourceOutcomeLike],
        pre_fusion_count: int,
        post_fusion_count: int,
        final_count: int,
        reranker_used: bool,
        reranker_degraded: bool,
    ) -> RetrievalDiagnostics:
        """Build full diagnostics from execution results.

        Args:
            status: Final completion status.
            total_duration_ms: Wall-clock duration of the phase.
            plan: The retrieval plan (provides selected sources).
            outcomes: List of source outcomes (success or failure).
            pre_fusion_count: Total items before fusion.
            post_fusion_count: Items after fusion.
            final_count: Items after selection.
            reranker_used: Whether the reranker was invoked.
            reranker_degraded: Whether the reranker fell back.
        """
        successful: list[str] = []
        stats_list: list[RetrievalSourceStats] = []
        failures_list: list[RetrievalSourceFailure] = []

        for outcome in outcomes:
            if outcome.stats is not None:
                stats_list.append(outcome.stats)
            if outcome.failure is not None:
                failures_list.append(outcome.failure)
                if outcome.stats is not None and not outcome.stats.timed_out:
                    continue
            if outcome.batch is not None:
                successful.append(outcome.route.source)

        return RetrievalDiagnostics(
            status=status,
            total_duration_ms=total_duration_ms,
            selected_sources=tuple(route.source for route in plan.routes),
            successful_sources=tuple(successful),
            source_stats=tuple(stats_list),
            failures=tuple(failures_list),
            pre_fusion_count=pre_fusion_count,
            post_fusion_count=post_fusion_count,
            final_count=final_count,
            reranker_used=reranker_used,
            reranker_degraded=reranker_degraded,
        )


class _SourceOutcomeLike:
    """Duck-typed interface for source outcomes.

    The actual SourceOutcome dataclass is defined in the phase
    implementation (infrastructure not imported here).  This
    protocol-compatible type lets the diagnostics builder stay
    in the pure component layer.
    """
    route: object
    batch: object | None
    stats: RetrievalSourceStats | None
    failure: RetrievalSourceFailure | None
