# cogito/agent/retrieval/selection.py
#
# RetrievalSelector — deterministic diversity-aware result selection.
#
# Applies per-kind and per-source quotas, then fills remaining slots
# in a second pass.  Ensures no single source or kind dominates the
# final result set.

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from cogito.agent.domain.retrieval import RetrievedItem, RetrievedItemKind


@dataclass(frozen=True, slots=True)
class RetrievalSelector:
    """Selects a diverse subset of retrieved items.

    Selection algorithm:
      1. First pass: strict quotas (max_per_kind, max_per_source).
      2. Second pass (if quota gaps remain): fill up to final_limit
         without quota restrictions, still deduplicated.
    """

    final_limit: int
    max_per_kind: int
    max_per_source: int

    def select(self, items: Sequence[RetrievedItem]) -> list[RetrievedItem]:
        """Select a diverse subset from the ranked items.

        Args:
            items: Items sorted by relevance descending.

        Returns:
            Selected items, still in descending relevance order.
        """
        selected: list[RetrievedItem] = []
        selected_ids: set[str] = set()
        kind_counts: Counter[RetrievedItemKind] = Counter()
        source_counts: Counter[str] = Counter()

        # First pass: strict quotas
        for item in items:
            if len(selected) >= self.final_limit:
                break
            if item.item_id in selected_ids:
                continue
            if kind_counts[item.kind] >= self.max_per_kind:
                continue
            if source_counts[item.source] >= self.max_per_source:
                continue

            selected.append(item)
            selected_ids.add(item.item_id)
            kind_counts[item.kind] += 1
            source_counts[item.source] += 1

        # Second pass: fill remaining slots without quota
        if len(selected) < self.final_limit:
            for item in items:
                if len(selected) >= self.final_limit:
                    break
                if item.item_id in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(item.item_id)

        return selected
