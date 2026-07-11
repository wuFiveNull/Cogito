"""P13-11: Unified RetrievalCandidate contract tests."""
from __future__ import annotations

from cogito.contracts.retrieval import RetrievalCandidate, RetrievalResult


class TestRetrievalCandidate:
    def test_immutable(self):
        c = RetrievalCandidate(
            candidate_type="memory", candidate_id="m1",
            final_score=0.8, retrieval_path="keyword",
        )
        assert c.candidate_type == "memory"
        assert c.final_score == 0.8
        # frozen
        with __import__("pytest").raises(AttributeError):
            c.final_score = 0.5  # type: ignore[misc]

    def test_source_refs_tuple(self):
        c = RetrievalCandidate(
            candidate_type="knowledge_segment",
            source_refs=["r1", "s1"],
        )
        assert isinstance(c.source_refs, tuple)
        assert c.source_refs == ("r1", "s1")

    def test_exclusion_reason(self):
        c = RetrievalCandidate(
            candidate_type="memory", candidate_id="m2",
            exclusion_reason="unauthorized",
        )
        assert c.exclusion_reason == "unauthorized"


class TestRetrievalResult:
    def test_result_immutable(self):
        cand = RetrievalCandidate(candidate_type="memory", candidate_id="m1")
        res = RetrievalResult(candidates=[cand], total_hits=1)
        assert len(res.candidates) == 1
        assert res.total_hits == 1
        assert isinstance(res.candidates, tuple)
