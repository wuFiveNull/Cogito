# cogito/agent/retrieval/fusion.py
#
# WeightedReciprocalRankFusion — cross-source fusion using RRF.
#
# Uses rank-based weighted Reciprocal Rank Fusion to merge results
# from heterogeneous retrieval sources whose raw scores are not
# directly comparable (BM25, cosine similarity, confidence, etc.).

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Sequence

from cogito.agent.domain.retrieval import (
    RetrievalBatch,
    RetrievalProvenance,
    RetrievedItem,
    RetrievalRoute,
)


@dataclass(frozen=True, slots=True)
class WeightedReciprocalRankFusion:
    """Fuses retrieval batches using Weighted Reciprocal Rank Fusion.

    The fusion score for a document d is::

        rrf(d) = Σ w_s / (k + rank_s(d))

    where:
      - w_s is the source weight from the route config
      - k is the RRF constant (default 60)
      - rank_s(d) is d's rank within source s (1-based)

    The final score is normalised to [0.0, 1.0] by dividing by the
    theoretical maximum RRF score (all sources at rank 1).
    """

    rrf_k: int = 60

    def merge(
        self,
        *,
        batches: Sequence[RetrievalBatch],
        routes: Sequence[RetrievalRoute],
    ) -> list[RetrievedItem]:
        """Merge batches from multiple sources into a single ranked list.

        Args:
            batches: One batch per source that returned results.
            routes: All configured routes (including failed ones).

        Returns:
            Deduplicated, merged items sorted by RRF score descending.
        """
        if not batches or not routes:
            return []

        route_by_source = {r.source: r for r in routes}
        score_by_key: dict[str, float] = defaultdict(float)
        items_by_key: dict[str, list[_RankedItem]] = defaultdict(list)

        # Accumulate RRF scores from each batch
        for batch in batches:
            route = route_by_source.get(batch.source)
            if route is None:
                continue

            for rank, item in enumerate(batch.items, start=1):
                key = self._require_dedupe_key(item)
                score_by_key[key] += route.weight / (self.rrf_k + rank)
                items_by_key[key].append(_RankedItem(rank=rank, item=item))

        if not items_by_key:
            return []

        # Theoretical maximum: all routes at rank 1
        max_score = (
            sum(r.weight / (self.rrf_k + 1) for r in routes) or 1.0
        )

        # Merge items with same dedupe key
        merged: list[RetrievedItem] = []
        for key, ranked_items in items_by_key.items():
            # Stable sort by rank, then source, then item_id
            ranked_items.sort(
                key=lambda pair: (
                    pair.rank,
                    pair.item.source,
                    pair.item.item_id,
                )
            )

            primary = ranked_items[0].item
            provenance = self._merge_provenance(ranked_items, batch_sources={b.source for b in batches})
            score = min(max(score_by_key[key] / max_score, 0.0), 1.0)

            merged.append(
                replace(
                    primary,
                    score=score,
                    dedupe_key=key,
                    provenance=provenance,
                )
            )

        # Sort by score DESC, kind, source, item_id (deterministic)
        merged.sort(
            key=lambda item: (
                -item.score,
                item.kind.value,
                item.source,
                item.item_id,
            )
        )

        return merged

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _require_dedupe_key(item: RetrievedItem) -> str:
        """Get or generate a dedupe key for fusion.

        The normaliser should have already generated dedupe_key before
        fusion.  If missing, fall back to item_id (which is still stable
        per adapter).
        """
        return item.dedupe_key or item.item_id

    @staticmethod
    def _merge_provenance(
        ranked_items: list[_RankedItem],
        batch_sources: set[str],
    ) -> tuple[RetrievalProvenance, ...]:
        """Merge provenance from all sources that returned this item.

        Collects the provenance stored on each copy (set by the
        normaliser) plus generates a provenance entry for any source
        the normaliser didn't annotate.
        """
        seen_sources: set[str] = set()
        merged: list[RetrievalProvenance] = []

        for ri in ranked_items:
            for prov in ri.item.provenance:
                if prov.source not in seen_sources:
                    seen_sources.add(prov.source)
                    merged.append(prov)

        # Add synthetic provenance for any source we have data from but
        # no provenance was recorded (fallback)
        for ri in ranked_items:
            if ri.item.source not in seen_sources:
                seen_sources.add(ri.item.source)
                merged.append(
                    RetrievalProvenance(
                        source=ri.item.source,
                        source_item_id=ri.item.item_id,
                        source_rank=ri.rank,
                    )
                )

        # Stable sort by source, then rank
        merged.sort(key=lambda p: (p.source, p.source_rank))
        return tuple(merged)


@dataclass(frozen=True, slots=True)
class _RankedItem:
    rank: int
    item: RetrievedItem
