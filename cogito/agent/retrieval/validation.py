# cogito/agent/retrieval/validation.py
#
# RetrievalItemValidator — validates retriever and reranker output.
#
# Contract checks ensure that adapters honour the RetrieverPort
# protocol.  Violations are raised as RetrievalResultValidationError,
# which the phase catches and records as source failures.

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from cogito.agent.domain.retrieval import (
    RetrievalBatch,
    RetrievalRoute,
    RetrievedItem,
)


@dataclass(frozen=True, slots=True)
class RetrievalResultValidationError(ValueError):
    """Retriever or reranker output violates the stable contract.

    This is a validation error, not a runtime error.  It signals
    an adapter bug or corrupted data, not a transient infrastructure
    failure.
    """
    message: str
    source: str = ""


class RetrievalItemValidator:
    """Validates retriever and reranker output against the port contract.

    All methods raise ``RetrievalResultValidationError`` on failure.
    """

    # ── Batch validation ──────────────────────────────────────────────

    def validate_batch(
        self,
        *,
        retriever_name: str,
        route: RetrievalRoute,
        batch: RetrievalBatch,
    ) -> None:
        """Validate a single-source batch against the route contract."""
        self._check_source_match(retriever_name, batch)
        self._check_limits(route, batch)
        for item in batch.items:
            self._check_item(item)

    # ── Reranker output validation ────────────────────────────────────

    def validate_reranked(
        self,
        *,
        inputs: Sequence[RetrievedItem],
        outputs: Sequence[RetrievedItem],
    ) -> None:
        """Validate reranker output against input set constraints."""
        input_ids = {item.item_id for item in inputs}

        for item in outputs:
            # Must be from input set
            if item.item_id not in input_ids:
                raise RetrievalResultValidationError(
                    f"Reranker returned item {item.item_id!r} "
                    f"not in input set",
                    source="reranker",
                )

        # No duplicates
        output_ids = [item.item_id for item in outputs]
        if len(output_ids) != len(set(output_ids)):
            raise RetrievalResultValidationError(
                "Reranker returned duplicates",
                source="reranker",
            )

    # ── Final result validation ───────────────────────────────────────

    @staticmethod
    def validate_final(items: Sequence[RetrievedItem]) -> None:
        """Validate final selected items before context write."""
        seen: set[str] = set()
        for item in items:
            if not 0.0 <= item.score <= 1.0:
                raise RetrievalResultValidationError(
                    f"Item {item.item_id!r} score {item.score} "
                    f"outside [0.0, 1.0]",
                    source="final",
                )
            if item.item_id in seen:
                raise RetrievalResultValidationError(
                    f"Duplicate item {item.item_id!r} in final output",
                    source="final",
                )
            seen.add(item.item_id)

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _check_source_match(
        retriever_name: str,
        batch: RetrievalBatch,
    ) -> None:
        if batch.source != retriever_name:
            raise RetrievalResultValidationError(
                f"Batch source {batch.source!r} != "
                f"retriever name {retriever_name!r}",
                source=retriever_name,
            )

    @staticmethod
    def _check_limits(route: RetrievalRoute, batch: RetrievalBatch) -> None:
        if len(batch.items) > route.limit:
            raise RetrievalResultValidationError(
                f"Retriever returned {len(batch.items)} items, "
                f"exceeds limit {route.limit}",
                source=route.source,
            )

    @staticmethod
    def _check_item(item: RetrievedItem) -> None:
        errors: list[str] = []

        if not item.item_id or not item.item_id.strip():
            errors.append("empty item_id")

        if not item.content or not item.content.strip():
            errors.append("empty content")

        if math.isnan(item.score) or math.isinf(item.score):
            errors.append(f"invalid score: {item.score}")

        if errors:
            raise RetrievalResultValidationError(
                f"Item {item.item_id!r}: {'; '.join(errors)}",
                source=item.source,
            )
