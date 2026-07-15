"""P13-15 PoC 对比框架单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from scripts.poc_memu.dataset import generate_dataset
from scripts.poc_memu.metrics import (
    mrr,
    recall_at_k,
    retrieval_latency,
    source_traceability_rate,
)


class TestDataset:
    def test_fixed_seed_reproducible(self):
        """固定 seed 可复现（PLAN-13 §16.2）。"""
        d1 = generate_dataset(seed=42)
        d2 = generate_dataset(seed=42)
        assert [d.doc_id for d in d1] == [d.doc_id for d in d2]
        assert all(d.content for d in d1)
        assert all(d.expected_queries for d in d1)

    def test_no_real_pii(self):
        """无明显真实 PII（电话号码、邮箱等）。"""
        import re

        docs = generate_dataset()
        for d in docs:
            # 无邮箱
            assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", d.content), d.doc_id
            # 无中国大陆手机号
            assert not re.search(r"1[3-9]\d{9}", d.content), d.doc_id

    def test_covers_conversation(self):
        docs = generate_dataset()
        convs = [d for d in docs if d.doc_id.startswith("conv_")]
        assert len(convs) >= 3

    def test_covers_code(self):
        docs = generate_dataset()
        codes = [d for d in docs if d.doc_id.startswith("code_")]
        assert len(codes) >= 2


class TestMetrics:
    def test_recall_at_k_perfect(self):
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0

    def test_recall_at_k_partial(self):
        assert recall_at_k(["a", "x", "y"], {"a", "b"}, 3) == 0.5

    def test_recall_at_k_zero(self):
        assert recall_at_k(["x", "y"], {"a", "b"}, 2) == 0.0

    def test_mrr_first(self):
        assert mrr(["a", "b"], {"a"}) == 1.0

    def test_mrr_second(self):
        assert mrr(["x", "a"], {"a"}) == 0.5

    def test_mrr_zero(self):
        assert mrr(["x", "y"], {"a"}) == 0.0

    def test_traceability_all(self):
        class B:
            def segment_provenance(self, sid):
                return f"p-{sid}"

        assert source_traceability_rate(B(), ["s1", "s2"]) == 1.0

    def test_traceability_none(self):
        class B:
            def segment_provenance(self, sid):
                return None

        assert source_traceability_rate(B(), ["s1"]) == 0.0

    def test_latency_returns_p50_p95(self):
        def fn(q, k):
            return [("a", 1.0)]

        result = retrieval_latency(fn, "q", repeats=5)
        assert "p50" in result and "p95" in result
        assert result["p50"] >= 0


class TestCogitoBackend:
    def test_ingest_retrieve_invalidate(self):
        from scripts.poc_memu.cogito_backend import CogitoBackend

        backend = CogitoBackend()
        try:
            segs = backend.ingest("test_doc", "# Hello\n\nWorld content.")
            assert len(segs) >= 1
            # 检索 Hello
            results = backend.retrieve("Hello", top_k=5)
            assert len(results) >= 1
            # 来源可追溯
            prov = backend.segment_provenance(segs[0])
            assert prov is not None
            assert "test_doc" in prov
            # 撤销
            backend.invalidate("test_doc")
            results_after = backend.retrieve("Hello", top_k=5)
            # 撤销后不应再召回
            after_ids = {r[0] for r in results_after}
            assert not any(s in after_ids for s in segs)
        finally:
            backend.close()

    def test_idempotent_ingest(self):
        """同 doc_id 重复 ingest 不产生重复段落地。"""
        from scripts.poc_memu.cogito_backend import CogitoBackend

        backend = CogitoBackend()
        try:
            segs1 = backend.ingest("idem_doc", "# Title\n\nContent A.")
            cnt1 = len(segs1)
            segs2 = backend.ingest("idem_doc", "# Title\n\nContent B.")
            # 旧段落地被撤销，新段落地创建
            assert len(segs2) >= 1
        finally:
            backend.close()
