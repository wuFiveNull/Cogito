# cogito/agent/ports/retrieval.py
#
# Port interfaces for InformationRetrievalPhase.
#
# Design rules (see information-retrieval-phase-final-design §6):
#   - RetrieverPort: one per retrieval source, named and unique.
#   - RetrievalFusionPort: merges batches from multiple sources.
#   - RetrievalRerankerPort: re-rank fused results, may degrade.
#   - RetrievalAccessFilterPort: defensive ACL check.
#   - All ports are Protocols — no concrete dependencies.

from __future__ import annotations

from typing import Protocol, Sequence

from cogito.agent.domain.retrieval import (
    RetrievalAccessContext,
    RetrievalBatch,
    RetrievalQuery,
    RetrievedItem,
    RetrievalRoute,
)


class RetrieverPort(Protocol):
    """Abstract retriever that searches a specific source.

    Contract:
      - ``name`` is unique and stable within a process.
      - ``retrieve()`` returns a ``RetrievalBatch`` whose ``.source``
        equals ``.name``.
      - Items are ordered by source-internal relevance (descending).
      - Adapter must NOT return more than ``limit`` items.
      - Adapter must perform source-side ACL filtering.
      - Adapter must NOT swallow ``asyncio.CancelledError``.
      - Empty results return an empty batch, never raise.
    """

    @property
    def name(self) -> str: ...

    async def retrieve(
        self,
        *,
        query: RetrievalQuery,
        limit: int,
    ) -> RetrievalBatch: ...


class RetrievalAccessFilterPort(Protocol):
    """Defensive ACL filter for retrieved items.

    Runs at two points in the pipeline:
      1. After each source returns (before fusion).
      2. After fusion (before reranker).
      3. After reranker (before selection).

    Default implementation can be a pure-memory ruleset.
    """

    async def filter(
        self,
        *,
        access: RetrievalAccessContext,
        items: Sequence[RetrievedItem],
    ) -> list[RetrievedItem]: ...


class RetrievalFusionPort(Protocol):
    """Merges results from multiple retrievers into a single ranked list.

    Must:
      - Deduplicate across sources using ``dedupe_key``.
      - Merge provenance chains.
      - Use source rank (not raw score) for cross-source comparison.
      - Output unified ``[0.0, 1.0]`` scores.
      - Produce deterministic ordering.
    """

    def merge(
        self,
        *,
        batches: Sequence[RetrievalBatch],
        routes: Sequence[RetrievalRoute],
    ) -> list[RetrievedItem]: ...


class RetrievalRerankerPort(Protocol):
    """Re-ranks retrieved items for relevance.

    Contract:
      - Only reorders and re-scores items from the input set.
      - Must NOT create new items or modify item_id/kind/content/source.
      - Must NOT return duplicates.
      - Must NOT return more than ``limit`` items.
      - Scores must be in ``[0.0, 1.0]``.
    """

    async def rerank(
        self,
        *,
        query: RetrievalQuery,
        items: Sequence[RetrievedItem],
        limit: int,
    ) -> list[RetrievedItem]: ...


class IdentityRetrievalReranker:
    """No-op reranker — returns items as-is up to limit.

    This is used when the system explicitly configures no re-ranking.
    It is NOT a stub for unimplemented logic.
    """

    async def rerank(
        self,
        *,
        query: RetrievalQuery,
        items: Sequence[RetrievedItem],
        limit: int,
    ) -> list[RetrievedItem]:
        return list(items[:limit])


class AllowAllAccessFilter:
    """Permissive filter that allows all items through.

    Suitable for single-actor personal agent scenarios.
    Multi-tenant deployments MUST provide a real implementation.
    """

    async def filter(
        self,
        *,
        access: RetrievalAccessContext,
        items: Sequence[RetrievedItem],
    ) -> list[RetrievedItem]:
        return list(items)
