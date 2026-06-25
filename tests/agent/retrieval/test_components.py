"""Unit tests for retrieval pure components."""

from __future__ import annotations

from datetime import datetime

import pytest

from cogito.agent.domain.retrieval import (
    RetrievalAccessContext,
    RetrievalBatch,
    RetrievalFilters,
    RetrievalProvenance,
    RetrievalQuery,
    RetrievalRoute,
    RetrievedItem,
    RetrievedItemKind,
)
from cogito.agent.retrieval.fusion import WeightedReciprocalRankFusion
from cogito.agent.retrieval.normalization import RetrievalNormalizer
from cogito.agent.retrieval.selection import RetrievalSelector
from cogito.agent.retrieval.validation import (
    RetrievalItemValidator,
    RetrievalResultValidationError,
)


# ═══════════════════════════════════════════════════════════════════════
# Fusion tests
# ═══════════════════════════════════════════════════════════════════════


class TestWeightedReciprocalRankFusion:
    def test_single_source_preserves_rank(self) -> None:
        fusion = WeightedReciprocalRankFusion(rrf_k=60)
        items = (
            _make_item("a-1", "doc A", score=0.9),
            _make_item("a-2", "doc B", score=0.5),
        )
        batches = [RetrievalBatch(source="keyword", items=items)]
        routes = [RetrievalRoute(source="keyword", limit=10, timeout_seconds=1.0, weight=1.0)]

        result = fusion.merge(batches=batches, routes=routes)

        assert len(result) == 2
        assert result[0].item_id == "a-1"
        assert result[1].item_id == "a-2"

    def test_cross_source_dedup(self) -> None:
        fusion = WeightedReciprocalRankFusion(rrf_k=60)
        item = _make_item("x-1", "duplicate content")
        batches = [
            RetrievalBatch(source="keyword", items=(item,)),
            RetrievalBatch(source="vector", items=(item,)),
        ]
        routes = [
            RetrievalRoute(source="keyword", limit=10, timeout_seconds=1.0, weight=1.0),
            RetrievalRoute(source="vector", limit=10, timeout_seconds=1.0, weight=1.0),
        ]

        result = fusion.merge(batches=batches, routes=routes)

        assert len(result) == 1
        assert result[0].score > 0

    def test_deterministic_ordering(self) -> None:
        fusion = WeightedReciprocalRankFusion(rrf_k=60)
        items1 = (_make_item("a", "doc A"), _make_item("b", "doc B"))
        items2 = (_make_item("c", "doc C"),)
        batches = [
            RetrievalBatch(source="keyword", items=items1),
            RetrievalBatch(source="vector", items=items2),
        ]
        routes = [
            RetrievalRoute(source="keyword", limit=10, timeout_seconds=1.0, weight=1.0),
            RetrievalRoute(source="vector", limit=10, timeout_seconds=1.0, weight=1.0),
        ]

        result1 = fusion.merge(batches=batches, routes=routes)
        result2 = fusion.merge(batches=batches, routes=routes)

        assert [r.item_id for r in result1] == [r.item_id for r in result2]

    def test_empty_batches(self) -> None:
        fusion = WeightedReciprocalRankFusion(rrf_k=60)
        result = fusion.merge(batches=[], routes=[])
        assert result == []

    def test_score_range(self) -> None:
        fusion = WeightedReciprocalRankFusion(rrf_k=60)
        items = (
            _make_item("a", "doc A", score=0.9),
            _make_item("b", "doc B", score=0.3),
        )
        batches = [RetrievalBatch(source="keyword", items=items)]
        routes = [RetrievalRoute(source="keyword", limit=10, timeout_seconds=1.0, weight=1.0)]

        result = fusion.merge(batches=batches, routes=routes)

        for item in result:
            assert 0.0 <= item.score <= 1.0


# ═══════════════════════════════════════════════════════════════════════
# Normalizer tests
# ═══════════════════════════════════════════════════════════════════════


class TestRetrievalNormalizer:
    def test_content_normalisation(self) -> None:
        normalizer = RetrievalNormalizer()
        item = _make_item("i-1", "  Hello　World\n\r\nNext  ")
        batch = RetrievalBatch(source="test", items=(item,))

        result = normalizer.normalize_batch(batch)

        assert len(result.items) == 1
        content = result.items[0].content
        assert "\r" not in content
        assert "\x00" not in content
        assert content.startswith("Hello")
        assert content.endswith("Next")

    def test_source_dedup(self) -> None:
        normalizer = RetrievalNormalizer()
        items = (
            _make_item("i-1", "same content", dedupe_key="dup:key"),
            _make_item("i-2", "same content", dedupe_key="dup:key"),
        )
        batch = RetrievalBatch(source="test", items=items)

        result = normalizer.normalize_batch(batch)

        assert len(result.items) == 1

    def test_max_content_chars(self) -> None:
        normalizer = RetrievalNormalizer()
        long_content = "x" * 500
        item = _make_item("i-1", long_content)
        batch = RetrievalBatch(source="test", items=(item,))

        result = normalizer.normalize_batch(batch, max_content_chars=100)

        assert len(result.items[0].content) == 100


# ═══════════════════════════════════════════════════════════════════════
# Selector tests
# ═══════════════════════════════════════════════════════════════════════


class TestRetrievalSelector:
    def test_final_limit(self) -> None:
        selector = RetrievalSelector(final_limit=3, max_per_kind=10, max_per_source=10)
        items = [_make_item(f"i-{i}", f"doc {i}") for i in range(10)]

        result = selector.select(items)

        assert len(result) == 3

    def test_max_per_kind(self) -> None:
        selector = RetrievalSelector(final_limit=2, max_per_kind=1, max_per_source=10)
        items = [
            _make_item("a", "doc A", kind=RetrievedItemKind.DOCUMENT),
            _make_item("b", "doc B", kind=RetrievedItemKind.DOCUMENT),
            _make_item("c", "doc C", kind=RetrievedItemKind.DOCUMENT),
            _make_item("d", "doc D", kind=RetrievedItemKind.HISTORY),
        ]

        result = selector.select(items)

        # final_limit=2 means only 2 items, max_per_kind=1 means only 1 DOCUMENT + 1 HISTORY
        assert len(result) == 2
        kinds = [r.kind for r in result]
        assert kinds.count(RetrievedItemKind.DOCUMENT) == 1
        assert kinds.count(RetrievedItemKind.HISTORY) == 1

    def test_second_pass_fills_gaps(self) -> None:
        selector = RetrievalSelector(final_limit=5, max_per_kind=1, max_per_source=10)
        items = [
            _make_item("a", "doc A", kind=RetrievedItemKind.MEMORY),
            _make_item("b", "doc B", kind=RetrievedItemKind.MEMORY),
            _make_item("c", "doc C", kind=RetrievedItemKind.MEMORY),
            _make_item("d", "doc D", kind=RetrievedItemKind.HISTORY),
            _make_item("e", "doc E", kind=RetrievedItemKind.HISTORY),
        ]

        result = selector.select(items)

        # First pass: a (memory), d (history) → 2 items. Second pass: b, c, e → 5
        assert len(result) == 5
        ids = [r.item_id for r in result]
        assert "a" in ids
        assert "d" in ids


# ═══════════════════════════════════════════════════════════════════════
# Validator tests
# ═══════════════════════════════════════════════════════════════════════


class TestRetrievalItemValidator:
    def test_rejects_source_mismatch(self) -> None:
        validator = RetrievalItemValidator()
        route = RetrievalRoute(source="keyword", limit=10, timeout_seconds=1.0, weight=1.0)
        batch = RetrievalBatch(source="vector", items=())

        with pytest.raises(RetrievalResultValidationError):
            validator.validate_batch(
                retriever_name="keyword",
                route=route,
                batch=batch,
            )

    def test_rejects_over_limit(self) -> None:
        validator = RetrievalItemValidator()
        route = RetrievalRoute(source="test", limit=2, timeout_seconds=1.0, weight=1.0)
        items = tuple(
            _make_item(f"i-{i}", f"doc {i}") for i in range(5)
        )
        batch = RetrievalBatch(source="test", items=items)

        with pytest.raises(RetrievalResultValidationError):
            validator.validate_batch(
                retriever_name="test",
                route=route,
                batch=batch,
            )

    def test_rejects_nan_score(self) -> None:
        validator = RetrievalItemValidator()
        import math
        bad_item = _make_item("bad", "bad content", score=float("nan"))
        with pytest.raises(RetrievalResultValidationError):
            validator.validate_final([bad_item])

    def test_rejects_reranker_extra_items(self) -> None:
        validator = RetrievalItemValidator()
        inputs = [_make_item("i-1", "doc A")]
        outputs = [
            _make_item("i-1", "doc A"),
            _make_item("i-2", "doc B"),  # not in input set
        ]

        with pytest.raises(RetrievalResultValidationError):
            validator.validate_reranked(inputs=inputs, outputs=outputs)

    def test_accepts_valid(self) -> None:
        validator = RetrievalItemValidator()
        route = RetrievalRoute(source="test", limit=10, timeout_seconds=1.0, weight=1.0)
        item = _make_item("good", "good content")
        batch = RetrievalBatch(source="test", items=(item,))

        validator.validate_batch(retriever_name="test", route=route, batch=batch)
        validator.validate_final([item])
        # No exception = pass


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_item(
    item_id: str,
    content: str,
    *,
    score: float = 0.5,
    kind: RetrievedItemKind = RetrievedItemKind.DOCUMENT,
    dedupe_key: str | None = None,
) -> RetrievedItem:
    return RetrievedItem(
        item_id=item_id,
        kind=kind,
        content=content,
        source="test",
        score=score,
        dedupe_key=dedupe_key or f"dedup:{item_id}",
    )
