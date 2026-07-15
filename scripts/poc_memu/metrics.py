"""评测指标（PLAN-13 P13-15 §16.3）。

recall@k / MRR / source_traceability / latency。
"""

from __future__ import annotations

import time


def recall_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float:
    """recall@k：前 k 命中相关文档的比例。"""
    if not relevant_ids:
        return 0.0
    top_k = retrieved_ids[:k]
    hits = sum(1 for rid in top_k if rid in relevant_ids)
    return hits / len(relevant_ids)


def mrr(retrieved_ids: list[str], relevant_ids: set[str]) -> float:
    """Mean Reciprocal Rank。"""
    for i, rid in enumerate(retrieved_ids, 1):
        if rid in relevant_ids:
            return 1.0 / i
    return 0.0


def source_traceability_rate(
    backend,
    segment_ids: list[str],
) -> float:
    """来源可追溯率：段落地是否可追溯到 doc_id。"""
    if not segment_ids:
        return 1.0
    traced = sum(1 for sid in segment_ids if backend.segment_provenance(sid))
    return traced / len(segment_ids)


def retrieval_latency(
    fn,
    query: str,
    top_k: int = 8,
    repeats: int = 10,
) -> dict[str, float]:
    """检索延迟 p50/p95（ms）。"""
    times: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn(query, top_k)
        times.append((time.perf_counter() - start) * 1000)
    times.sort()
    p50 = times[len(times) // 2]
    p95 = times[int(len(times) * 0.95)] if len(times) > 1 else times[0]
    return {"p50": round(p50, 3), "p95": round(p95, 3)}


def evaluate_backend(
    backend,
    dataset,
    top_k: int = 8,
) -> dict:
    """对后端跑完整评测，返回指标。"""
    # 建立 doc_id → expected_queries 映射
    doc_to_queries = {d.doc_id: d for d in dataset}
    # 建立 query → relevant doc_ids 映射
    query_to_relevant: dict[str, set[str]] = {}
    for d in dataset:
        for q in d.expected_queries:
            query_to_relevant.setdefault(q, set()).add(d.doc_id)

    # 摄入所有文档
    doc_segment_map: dict[str, list[str]] = {}  # doc_id → [segment_id]
    for d in dataset:
        segs = backend.ingest(d.doc_id, d.content)
        doc_segment_map[d.doc_id] = segs

    # 检索评测
    recalls: list[float] = []
    mrrs: list[float] = []
    all_retrieved_segs: list[str] = []
    for query, relevant_docs in query_to_relevant.items():
        # 相关段落地 = 相关 doc 的所有 segment
        relevant_segs: set[str] = set()
        for did in relevant_docs:
            relevant_segs.update(doc_segment_map.get(did, []))
        retrieved = backend.retrieve(query, top_k)
        retrieved_ids = [rid for rid, _ in retrieved]
        all_retrieved_segs.extend(retrieved_ids)
        recalls.append(recall_at_k(retrieved_ids, relevant_segs, top_k))
        mrrs.append(mrr(retrieved_ids, relevant_segs))

    # 删除场景：撤销 delete_me 后不应再召回
    delete_recall = None
    if "delete_me" in doc_segment_map:
        backend.invalidate("delete_me")
        delete_queries = doc_to_queries["delete_me"].expected_queries
        for q in delete_queries:
            retrieved = backend.retrieve(q, top_k)
            retrieved_ids = [rid for rid, _ in retrieved]
            # delete_me 的段落地不应出现
            delete_segs = set(doc_segment_map.get("delete_me", []))
            if any(rid in delete_segs for rid in retrieved_ids):
                delete_recall = True
                break
        delete_recall = False if delete_recall is None else delete_recall

    return {
        "recall@k": round(sum(recalls) / len(recalls), 4) if recalls else 0,
        "mrr": round(sum(mrrs) / len(mrrs), 4) if mrrs else 0,
        "source_traceability": round(source_traceability_rate(backend, all_retrieved_segs), 4),
        "delete_no_resurrect": not (delete_recall or False),
        "num_docs": len(dataset),
        "num_segments": sum(len(s) for s in doc_segment_map.values()),
        "top_k": top_k,
    }
