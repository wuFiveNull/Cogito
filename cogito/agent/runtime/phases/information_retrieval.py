# cogito/agent/runtime/phases/information_retrieval.py
#
# InformationRetrievalPhase — Phase 3 of the 8-phase pipeline.
#
# Orchestrates concurrent retrieval from multiple sources, validates,
# fuses, re-ranks, and selects the final set of items for the turn.
#
# Design rules (see information-retrieval-phase-final-design §18):
#   - Does NOT build model messages, allocate token budget, or call LLM.
#   - Does NOT execute tools or persist data.
#   - Does NOT publish MessageBus messages.
#   - ctx.retrieved_items and ctx.retrieval_diagnostics are atomically
#     written at the end — no partial state on failure.
#   - CancelledError propagates without capture or wrapping.

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from cogito.agent.domain.retrieval import (
    RetrievalBatch,
    RetrievalCompletionStatus,
    RetrievalDiagnostics,
    RetrievalPlan,
    RetrievalProvenance,
    RetrievedItem,
    RetrievalRoute,
    RetrievalSourceFailure,
    RetrievalSourceStats,
)
from cogito.agent.ports.retrieval import (
    RetrievalAccessFilterPort,
    RetrievalFusionPort,
    RetrievalRerankerPort,
    RetrieverPort,
)
from cogito.agent.retrieval.diagnostics import RetrievalDiagnosticsBuilder
from cogito.agent.retrieval.normalization import RetrievalNormalizer
from cogito.agent.retrieval.query_builder import RetrievalQueryBuilder
from cogito.agent.retrieval.routing import RetrievalPhaseConfig, RetrievalRoutingPolicy
from cogito.agent.retrieval.selection import RetrievalSelector
from cogito.agent.retrieval.validation import (
    RetrievalItemValidator,
    RetrievalResultValidationError,
)
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    AllRetrievalSourcesFailedError,
    DuplicateRetrieverNameError,
    RequiredRetrievalSourceError,
    RetrievalConfigurationError,
    RetrievalPhaseTimeoutError,
    RetrievalRerankError,
)
from cogito.agent.runtime.events import AgentEventType
from cogito.agent.runtime.phase import BasePhase

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Internal outcome type (not exported)
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class _SourceOutcome:
    route: RetrievalRoute
    batch: RetrievalBatch | None
    stats: RetrievalSourceStats | None
    failure: RetrievalSourceFailure | None


# ═══════════════════════════════════════════════════════════════════════
# Phase
# ═══════════════════════════════════════════════════════════════════════


class InformationRetrievalPhase(BasePhase):
    """Phase 3: Retrieve relevant information from configured sources.

    Responsibilities:
    - Build a strongly typed RetrievalQuery from the turn context.
    - Route to enabled sources per configuration.
    - Execute sources concurrently with timeout and semaphore limits.
    - Validate, normalise, and deduplicate source results.
    - Fuse cross-source results via Weighted RRF.
    - Optionally re-rank with configurable fail-open.
    - Apply diversity quotas and final selection.
    - Write final items and diagnostics atomically to TurnContext.
    """

    name = "information_retrieval"

    def __init__(
        self,
        *,
        retrievers: Sequence[RetrieverPort],
        query_builder: RetrievalQueryBuilder,
        routing_policy: RetrievalRoutingPolicy,
        validator: RetrievalItemValidator,
        access_filter: RetrievalAccessFilterPort,
        normalizer: RetrievalNormalizer,
        fusion: RetrievalFusionPort,
        reranker: RetrievalRerankerPort,
        selector: RetrievalSelector,
        diagnostics_builder: RetrievalDiagnosticsBuilder,
        config: RetrievalPhaseConfig,
    ) -> None:
        self._retrievers = self._index_retrievers(retrievers)
        self._query_builder = query_builder
        self._routing_policy = routing_policy
        self._validator = validator
        self._access_filter = access_filter
        self._normalizer = normalizer
        self._fusion = fusion
        self._reranker = reranker
        self._selector = selector
        self._diagnostics_builder = diagnostics_builder
        self._config = config

        self._validate_configuration()

    # ═══════════════════════════════════════════════════════════════════
    # Execute
    # ═══════════════════════════════════════════════════════════════════

    async def execute(self, ctx: TurnContext) -> None:
        """Execute the retrieval phase for one turn.

        Args:
            ctx: Turn context.  Must have turn_id set (by TurnInitPhase)
                and session/user state loaded (by StateLoadPhase).

        Raises:
            RetrievalPhaseTimeoutError: Phase total timeout exceeded.
            RequiredRetrievalSourceError: A required source failed.
            AllRetrievalSourcesFailedError: Every enabled source failed.
        """
        started = perf_counter()

        # 1. Build query
        query = self._query_builder.build(ctx)

        # 2. Build plan
        plan = self._routing_policy.create_plan(query)

        # 3. No routes → empty result
        if not plan.routes:
            ctx.retrieved_items = []
            ctx.retrieval_diagnostics = self._diagnostics_builder.empty(
                total_duration_ms=self._elapsed_ms(started),
            )
            await self._emit_retrieval_completed(ctx)
            return

        # 4. Emit started event
        await self._emit_retrieval_started(ctx, plan)

        # 5. Concurrent execution
        try:
            outcomes = await self._execute_routes(plan)
        except TimeoutError:
            raise RetrievalPhaseTimeoutError(
                "Information retrieval phase timed out",
                safe_message="Information retrieval timed out",
            )

        # 6. Evaluate failures
        failures = [o.failure for o in outcomes if o.failure is not None]
        required_failures = [
            o for o in outcomes
            if o.route.required and o.failure is not None
        ]

        if required_failures:
            names = ", ".join(o.route.source for o in required_failures)
            raise RequiredRetrievalSourceError(
                f"Required retrieval sources failed: {names}",
                safe_message="A required retrieval source is unavailable",
            )

        successful_batches = [o.batch for o in outcomes if o.batch is not None]
        if not successful_batches:
            raise AllRetrievalSourcesFailedError(
                "All configured retrieval sources failed",
                safe_message="Information retrieval is temporarily unavailable",
            )

        pre_fusion_count = sum(len(b.items) for b in successful_batches)

        # 7. Fuse
        fused = self._fusion.merge(
            batches=successful_batches,
            routes=plan.routes,
        )

        # 8. Post-fusion ACL
        fused = await self._access_filter.filter(
            access=query.access,
            items=fused,
        )
        post_fusion_count = len(fused)

        # 9. Rerank
        reranker_used = bool(fused)
        reranker_degraded = False
        reranked: list[RetrievedItem]

        if fused:
            candidates = fused[: self._config.rerank_candidate_limit]
            try:
                async with asyncio.timeout(self._config.reranker_timeout_seconds):
                    reranked = await self._reranker.rerank(
                        query=query,
                        items=candidates,
                        limit=self._config.rerank_candidate_limit,
                    )
                self._validator.validate_reranked(inputs=candidates, outputs=reranked)
            except RetrievalResultValidationError:
                if not self._config.reranker_fail_open:
                    raise RetrievalRerankError(
                        "Retrieval reranker produced invalid output",
                        safe_message="Information reranking failed",
                    )
                logger.warning("Retrieval reranker produced invalid output — degraded")
                reranked = candidates
                reranker_degraded = True
            except Exception:
                if not self._config.reranker_fail_open:
                    raise RetrievalRerankError(
                        "Retrieval reranker failed",
                        safe_message="Information reranking failed",
                    )
                logger.exception("Retrieval reranker degraded")
                reranked = candidates
                reranker_degraded = True
        else:
            reranked = []

        # 10. Post-rerank ACL
        reranked = await self._access_filter.filter(
            access=query.access,
            items=reranked,
        )

        # 11. Diversity selection
        final_items = self._selector.select(reranked)
        self._validator.validate_final(final_items)

        # 12. Resolve status
        status = self._resolve_status(
            final_items=final_items,
            failures=failures,
            reranker_degraded=reranker_degraded,
        )

        # 13. Atomic context write
        ctx.retrieved_items = final_items
        ctx.retrieval_diagnostics = self._diagnostics_builder.build(
            status=status,
            total_duration_ms=self._elapsed_ms(started),
            plan=plan,
            outcomes=outcomes,
            pre_fusion_count=pre_fusion_count,
            post_fusion_count=post_fusion_count,
            final_count=len(final_items),
            reranker_used=reranker_used,
            reranker_degraded=reranker_degraded,
        )

        # 14. Emit completed event
        await self._emit_retrieval_completed(ctx)

    # ═══════════════════════════════════════════════════════════════════
    # Concurrent route execution
    # ═══════════════════════════════════════════════════════════════════

    async def _execute_routes(
        self,
        plan: RetrievalPlan,
    ) -> list[_SourceOutcome]:
        """Execute all routes concurrently under phase-level timeout.

        Each source runs in its own TaskGroup task.  Individual source
        errors are captured and returned as ``_SourceOutcome`` —
        only the phase-level TimeoutError propagates.
        """
        semaphore = asyncio.Semaphore(self._config.max_concurrency)

        async with asyncio.timeout(self._config.phase_timeout_seconds):
            async with asyncio.TaskGroup() as tg:
                tasks: dict[str, asyncio.Task[_SourceOutcome]] = {}
                for route in plan.routes:
                    retriever = self._retrievers.get(route.source)
                    if retriever is None:
                        logger.warning(
                            "Route %r has no matching retriever",
                            route.source,
                        )
                        continue
                    tasks[route.source] = tg.create_task(
                        self._execute_source(
                            retriever=retriever,
                            route=route,
                            query=plan.query,
                            semaphore=semaphore,
                        ),
                        name=f"retrieval:{route.source}",
                    )

            results: list[_SourceOutcome] = []
            for route in plan.routes:
                task = tasks.get(route.source)
                if task is None:
                    results.append(
                        _SourceOutcome(
                            route=route,
                            batch=None,
                            stats=None,
                            failure=RetrievalSourceFailure(
                                source=route.source,
                                kind="unavailable",
                                error_code="RETRIEVAL_SOURCE_NOT_CONFIGURED",
                                safe_message=f"Retrieval source '{route.source}' is not configured",
                                retryable=False,
                                duration_ms=0,
                            ),
                        )
                    )
                else:
                    results.append(task.result())

        return results

    async def _execute_source(
        self,
        *,
        retriever: RetrieverPort,
        route: RetrievalRoute,
        query: RetrievalQuery,
        semaphore: asyncio.Semaphore,
    ) -> _SourceOutcome:
        """Execute a single retrieval source.

        Catches individual source errors and returns a ``_SourceOutcome``
        without letting exceptions propagate to the TaskGroup (which would
        cancel all sibling tasks).
        """
        started = perf_counter()

        try:
            async with semaphore:
                async with asyncio.timeout(route.timeout_seconds):
                    batch = await retriever.retrieve(
                        query=query,
                        limit=route.limit,
                    )

            # Validate batch contract
            self._validator.validate_batch(
                retriever_name=retriever.name,
                route=route,
                batch=batch,
            )

            # Defensive ACL filter
            accepted = await self._access_filter.filter(
                access=query.access,
                items=batch.items,
            )

            # Normalise and deduplicate within source
            prepared = self._normalizer.normalize_batch(
                batch=RetrievalBatch(
                    source=batch.source,
                    items=tuple(accepted),
                    partial=batch.partial,
                ),
                max_content_chars=self._config.max_content_chars,
            )

            duration_ms = self._elapsed_ms(started)
            return _SourceOutcome(
                route=route,
                batch=prepared,
                stats=RetrievalSourceStats(
                    source=route.source,
                    duration_ms=duration_ms,
                    received_count=len(batch.items),
                    accepted_count=len(prepared.items),
                    rejected_count=len(batch.items) - len(prepared.items),
                ),
                failure=None,
            )

        except TimeoutError:
            duration_ms = self._elapsed_ms(started)
            return _SourceOutcome(
                route=route,
                batch=None,
                stats=RetrievalSourceStats(
                    source=route.source,
                    duration_ms=duration_ms,
                    received_count=0,
                    accepted_count=0,
                    rejected_count=0,
                    timed_out=True,
                ),
                failure=RetrievalSourceFailure(
                    source=route.source,
                    kind="timeout",
                    error_code="RETRIEVAL_SOURCE_TIMEOUT",
                    safe_message=f"Retrieval source '{route.source}' timed out",
                    retryable=True,
                    duration_ms=duration_ms,
                ),
            )

        except RetrievalResultValidationError:
            duration_ms = self._elapsed_ms(started)
            return _SourceOutcome(
                route=route,
                batch=None,
                stats=RetrievalSourceStats(
                    source=route.source,
                    duration_ms=duration_ms,
                    received_count=0,
                    accepted_count=0,
                    rejected_count=0,
                ),
                failure=RetrievalSourceFailure(
                    source=route.source,
                    kind="invalid_response",
                    error_code="RETRIEVAL_SOURCE_INVALID_RESPONSE",
                    safe_message=f"Retrieval source '{route.source}' returned invalid data",
                    retryable=False,
                    duration_ms=duration_ms,
                ),
            )

        except Exception:
            logger.exception(
                "Unexpected retrieval source failure: %s",
                route.source,
            )
            duration_ms = self._elapsed_ms(started)
            return _SourceOutcome(
                route=route,
                batch=None,
                stats=RetrievalSourceStats(
                    source=route.source,
                    duration_ms=duration_ms,
                    received_count=0,
                    accepted_count=0,
                    rejected_count=0,
                ),
                failure=RetrievalSourceFailure(
                    source=route.source,
                    kind="internal",
                    error_code="RETRIEVAL_SOURCE_INTERNAL_ERROR",
                    safe_message=f"Retrieval source '{route.source}' failed",
                    retryable=False,
                    duration_ms=duration_ms,
                ),
            )

    # ═══════════════════════════════════════════════════════════════════
    # Event emission
    # ═══════════════════════════════════════════════════════════════════

    async def _emit_retrieval_started(
        self,
        ctx: TurnContext,
        plan: RetrievalPlan,
    ) -> None:
        emitter = ctx.event_emitter
        if emitter is None:
            return
        try:
            await emitter.emit(
                event_type=AgentEventType.RETRIEVAL_STARTED,
                phase=self.name,
                data={
                    "selected_sources": [r.source for r in plan.routes],
                    "requested_limit": plan.query.limit,
                },
            )
        except Exception:
            logger.exception("Failed to emit RETRIEVAL_STARTED")

    async def _emit_retrieval_completed(self, ctx: TurnContext) -> None:
        emitter = ctx.event_emitter
        diag = ctx.retrieval_diagnostics
        if emitter is None or diag is None:
            return
        try:
            await emitter.emit(
                event_type=AgentEventType.RETRIEVAL_COMPLETED,
                phase=self.name,
                data={
                    "status": diag.status.value,
                    "duration_ms": diag.total_duration_ms,
                    "successful_sources": list(diag.successful_sources),
                    "failed_sources": [f.source for f in diag.failures],
                    "pre_fusion_count": diag.pre_fusion_count,
                    "post_fusion_count": diag.post_fusion_count,
                    "final_count": diag.final_count,
                    "reranker_used": diag.reranker_used,
                    "reranker_degraded": diag.reranker_degraded,
                },
            )
        except Exception:
            logger.exception("Failed to emit RETRIEVAL_COMPLETED")

    # ═══════════════════════════════════════════════════════════════════
    # Configuration & validation
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _index_retrievers(
        retrievers: Sequence[RetrieverPort],
    ) -> dict[str, RetrieverPort]:
        """Index retrievers by name, validating uniqueness."""
        indexed: dict[str, RetrieverPort] = {}
        for retriever in retrievers:
            name = retriever.name.strip()
            if not name:
                raise DuplicateRetrieverNameError(
                    "Retriever name must not be empty",
                )
            if name in indexed:
                raise DuplicateRetrieverNameError(
                    f"Duplicate retriever name: {name}",
                )
            indexed[name] = retriever
        return indexed

    def _validate_configuration(self) -> None:
        """Validate that all enabled sources have matching retrievers."""
        errors: list[str] = []
        for source_name, source_config in self._config.sources.items():
            if source_config.enabled and source_name not in self._retrievers:
                errors.append(
                    f"Source {source_name!r} enabled but no retriever injected",
                )

        invalid_sources = set(self._retrievers.keys()) - set(self._config.sources.keys())
        if invalid_sources:
            errors.append(
                f"Injected retrievers without config: {invalid_sources}",
            )

        if errors:
            raise RetrievalConfigurationError(
                "; ".join(errors),
                safe_message="Information retrieval configuration is invalid",
            )

    # ═══════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _resolve_status(
        *,
        final_items: Sequence[RetrievedItem],
        failures: Sequence[Any],
        reranker_degraded: bool,
    ) -> RetrievalCompletionStatus:
        if failures or reranker_degraded:
            return RetrievalCompletionStatus.DEGRADED
        if not final_items:
            return RetrievalCompletionStatus.EMPTY
        return RetrievalCompletionStatus.COMPLETED

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((perf_counter() - started) * 1000)
