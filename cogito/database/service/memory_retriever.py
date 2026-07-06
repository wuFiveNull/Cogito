"""
cogito.database.service.memory_retriever — 记忆检索

提供三类检索函数，可独立使用或通过 MemoryRetriever 调用。

使用方式:
    # 独立函数（直接传入 db）
    from cogito.database.service.memory_retriever import (
        keyword_search,
        vector_search,
        hybrid_search,
        deserialize_embedding,
        serialize_embedding,
    )
    results = await keyword_search(db, user_id="u1", query="杭州餐厅")

    # 或通过 DatabaseManager
    results = await db.memory_retriever.keyword_search("杭州餐厅")
"""

from __future__ import annotations

import json
import struct
from typing import Any

from cogito.database.connection import AsyncDatabase
from cogito.database.repository.memories import MemoryRepository
from cogito.database.service.trace_service import TraceService
from cogito.database.utils import utcnow

# ═══════════════════════════════════════════════════════════════════
#  Embedding 工具函数
# ═══════════════════════════════════════════════════════════════════


def serialize_embedding(vector: list[float]) -> bytes:
    """将 Float32 列表序列化为 Little-Endian BLOB。

    Args:
        vector: Float32 向量

    Returns:
        可用于存入 memories.embedding 的 BLOB
    """
    return struct.pack(f"<{len(vector)}f", *vector)


def deserialize_embedding(blob: bytes) -> list[float]:
    """从 BLOB 反序列化 Float32 LE 向量。

    Args:
        blob: memories.embedding 字段的原始字节

    Returns:
        Float32 列表
    """
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度（要求已 L2 归一化则等价于点积）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ═══════════════════════════════════════════════════════════════════
#  关键词检索 (FTS5 + LIKE 回退)
# ═══════════════════════════════════════════════════════════════════


async def keyword_search(
    db: AsyncDatabase,
    user_id: str,
    query: str,
    *,
    query_time: str | None = None,
    limit: int = 30,
    repo: MemoryRepository | None = None,
) -> list[dict[str, Any]]:
    """关键词检索记忆。

    自动选择检索方式:
    - 查询 >= 3 字符 → FTS5 trigram 全文检索
    - 查询 < 3 字符   → LIKE 子串回退

    Args:
        db: 数据库连接
        user_id: 用户 ID
        query: 搜索关键词
        query_time: ISO 8601 时间，用于时间窗口过滤，默认当前 UTC 时间
        limit: 最大返回数量
        repo: 可选，复用已创建的 MemoryRepository

    Returns:
        匹配的记忆记录列表
    """
    r = repo or MemoryRepository(db)
    qt = query_time or utcnow()

    q = query.strip()
    if not q:
        return []

    if len(q) < 3:
        # 短词 → LIKE 回退
        return await r.search_like(user_id, q, limit=min(limit, 20))

    # 长词 → FTS5 trigram
    return await r.search_fts(user_id, q, qt, limit=limit)


async def keyword_search_multi(
    db: AsyncDatabase,
    user_id: str,
    queries: list[str],
    *,
    query_time: str | None = None,
    limit: int = 30,
    repo: MemoryRepository | None = None,
) -> list[dict[str, Any]]:
    """多关键词检索（合并结果，去重）。

    Args:
        db: 数据库连接
        user_id: 用户 ID
        queries: 多个搜索词
        query_time: ISO 8601 时间
        limit: 最大返回数量
        repo: 可选，复用 MemoryRepository

    Returns:
        去重后的记忆记录列表（保持相关性排序）
    """
    r = repo or MemoryRepository(db)
    qt = query_time or utcnow()

    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    for q in queries:
        batch = await keyword_search(db, user_id, q, query_time=qt, limit=limit, repo=r)
        for mem in batch:
            mid = mem["id"]
            if mid not in seen:
                seen.add(mid)
                results.append(mem)

    return results[:limit]


# ═══════════════════════════════════════════════════════════════════
#  向量检索 (BLOB 应用层精确计算)
# ═══════════════════════════════════════════════════════════════════


async def vector_search(
    db: AsyncDatabase,
    user_id: str,
    query_embedding: list[float],
    *,
    query_time: str | None = None,
    top_k: int = 30,
    repo: MemoryRepository | None = None,
) -> list[dict[str, Any]]:
    """向量检索 — 应用层精确余弦相似度计算。

    从数据库读取该用户所有含 embedding 的 active 记忆，
    在 Python 层计算余弦相似度后排序返回 Top-K。

    Args:
        db: 数据库连接
        user_id: 用户 ID
        query_embedding: 查询向量 (Float32 list, 建议 L2 归一化)
        query_time: ISO 8601 时间，用于时间窗口过滤
        top_k: 最大返回数量
        repo: 可选，复用 MemoryRepository

    Returns:
        按相似度降序排列的记忆记录列表（每条含 similarity 字段）
    """
    r = repo or MemoryRepository(db)
    qt = query_time or utcnow()

    candidates = await r.get_active_embeddings(user_id, qt)
    if not candidates:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for c in candidates:
        emb_bytes: bytes | None = c.get("embedding")
        if emb_bytes is None:
            continue

        emb = deserialize_embedding(emb_bytes)
        if len(emb) != len(query_embedding):
            continue

        sim = cosine_similarity(emb, query_embedding)
        scored.append((sim, c))

    scored.sort(key=lambda x: x[0], reverse=True)

    result = []
    for sim, mem in scored[:top_k]:
        mem["similarity"] = round(sim, 6)
        result.append(mem)

    return result


# ═══════════════════════════════════════════════════════════════════
#  混合检索 (RRF 融合)
# ═══════════════════════════════════════════════════════════════════


async def hybrid_search(
    db: AsyncDatabase,
    user_id: str,
    *,
    keywords: list[str] | None = None,
    semantic_queries: list[str] | None = None,
    memory_types: list[str] | None = None,
    query_embedding: list[float] | None = None,
    query_time: str | None = None,
    top_k: int = 10,
    repo: MemoryRepository | None = None,
) -> list[dict[str, Any]]:
    """混合检索：多通道召回 + RRF 融合 + 业务加权排序。

    支持 5 个通道:
    - key:     精确 memory_key 匹配
    - fts:     FTS5 trigram 全文检索 (≥3字符)
    - like:    LIKE 子串回退 (<3字符)
    - semantic_fts: 语义查询 FTS (额外语义维度)
    - vector:  向量余弦相似度

    融合公式:
        final_score = 0.70 × normalized_rrf
                    + 0.15 × importance
                    + 0.10 × confidence
                    + 0.05 × exact_key_bonus

    Args:
        db: 数据库连接
        user_id: 用户 ID
        keywords: 关键词列表
        semantic_queries: 语义查询列表（额外的 FTS 通道）
        memory_types: 只返回指定类型的记忆
        query_embedding: 查询向量
        query_time: ISO 8601 时间
        top_k: 最终返回数量
        repo: 可选，复用 MemoryRepository

    Returns:
        按综合分降序排列的记忆记录列表
    """
    r = repo or MemoryRepository(db)
    qt = query_time or utcnow()

    # ── 多通道召回 ─────────────────────────────────────────────
    all_candidates: dict[str, _Candidate] = {}
    channels_used: list[str] = []

    # 通道 1: 精确 Key 查询
    if keywords:
        for kw in keywords:
            mem = await r.get_active_by_key(user_id, kw)
            if mem:
                _add_candidate(all_candidates, mem, "key", rank=1)
                channels_used.append("key")

    # 通道 2: FTS5 全文检索
    if keywords:
        for kw in keywords:
            q = kw.strip()
            if len(q) >= 3:
                fts_results = await r.search_fts(user_id, q, qt, limit=30)
                for rank, mem in enumerate(fts_results):
                    _add_candidate(all_candidates, mem, "fts", rank=rank + 1)
                channels_used.append("fts")

    # 通道 3: 短词回退 LIKE
    if keywords:
        for kw in keywords:
            q = kw.strip()
            if len(q) < 3:
                like_results = await r.search_like(user_id, q, limit=20)
                for rank, mem in enumerate(like_results):
                    _add_candidate(all_candidates, mem, "like", rank=rank + 1)
                channels_used.append("like")

    # 通道 4: 语义查询 FTS
    if semantic_queries:
        for sq in semantic_queries:
            q = sq.strip()
            if len(q) >= 3:
                sem_results = await r.search_fts(user_id, q, qt, limit=30)
                for rank, mem in enumerate(sem_results):
                    _add_candidate(all_candidates, mem, "semantic_fts", rank=rank + 1)
                channels_used.append("semantic_fts")

    # 通道 5: 向量检索
    if query_embedding:
        vector_results = await vector_search(
            db, user_id, query_embedding, query_time=qt, top_k=30, repo=r,
        )
        for rank, mem in enumerate(vector_results):
            _add_candidate(all_candidates, mem, "vector", rank=rank + 1)
        channels_used.append("vector")

    if not all_candidates:
        return []

    # ── RRF 融合 ───────────────────────────────────────────────
    k = 60.0
    for c in all_candidates.values():
        rrf_score = sum(1.0 / (k + r) for r in c.ranks)
        importance_score = c.memory.get("importance", 0.5)
        confidence_score = c.memory.get("confidence", 0.8)
        exact_key_bonus = 0.05 if "key" in c.channels else 0.0

        c.final_score = (
            0.70 * (rrf_score / max(len(channels_used), 1))
            + 0.15 * importance_score
            + 0.10 * confidence_score
            + 0.05 * exact_key_bonus
        )

    # ── 排序 ───────────────────────────────────────────────────
    sorted_candidates = sorted(
        all_candidates.values(),
        key=lambda x: x.final_score,
        reverse=True,
    )

    # ── 类型过滤 + 截断 ────────────────────────────────────────
    result = []
    type_set = set(memory_types) if memory_types else None
    for c in sorted_candidates:
        if type_set and c.memory.get("memory_type") not in type_set:
            continue
        c.memory["final_score"] = round(c.final_score, 4)
        result.append(c.memory)
        if len(result) >= top_k:
            break

    return result


# ═══════════════════════════════════════════════════════════════════
#  MemoryRetriever 类（委托到独立函数，保持向后兼容）
# ═══════════════════════════════════════════════════════════════════


class MemoryRetriever:
    """记忆检索服务（委托到独立函数）。

    推荐直接使用独立函数:
        keyword_search(db, user_id, query)
        vector_search(db, user_id, query_embedding)
        hybrid_search(db, user_id, ...)
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db
        self._repo = MemoryRepository(db)

    async def retrieve_by_key(self, user_id: str, memory_key: str) -> dict[str, Any] | None:
        """精确 Key 查询。"""
        return await self._repo.get_active_by_key(user_id, memory_key)

    async def retrieve_by_type(self, user_id: str, memory_type: str, limit: int = 50) -> list[dict[str, Any]]:
        """按类型查询。"""
        return await self._repo.get_active_by_type(user_id, memory_type, limit=limit)

    async def keyword_search(self, query: str, *, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """关键词检索（对 user_id 字段自动补全）。"""
        return await keyword_search(self._db, user_id, query, limit=limit, repo=self._repo)

    async def vector_search(self, query_embedding: list[float], *, user_id: str, top_k: int = 30) -> list[dict[str, Any]]:
        """向量检索。"""
        return await vector_search(self._db, user_id, query_embedding, top_k=top_k, repo=self._repo)

    async def hybrid_search(
        self,
        *,
        user_id: str,
        keywords: list[str] | None = None,
        semantic_queries: list[str] | None = None,
        memory_types: list[str] | None = None,
        query_embedding: list[float] | None = None,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """混合检索。"""
        return await hybrid_search(
            self._db, user_id,
            keywords=keywords,
            semantic_queries=semantic_queries,
            memory_types=memory_types,
            query_embedding=query_embedding,
            top_k=top_k,
            repo=self._repo,
        )

    async def record_retrieval_span(
        self,
        *,
        trace_id: str,
        parent_span_id: str,
        user_id: str,
        session_id: str | None = None,
        query_analysis: dict[str, Any] | None = None,
        candidate_ids: list[str] | None = None,
        selected_ids: list[str] | None = None,
        channel_info: dict[str, list[str]] | None = None,
        scores: dict[str, float] | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """记录检索链路 span。"""
        trace_service = TraceService(self._db)
        metadata: dict[str, Any] = {}
        if candidate_ids is not None:
            metadata["candidate_memory_ids"] = candidate_ids
        if selected_ids is not None:
            metadata["selected_memory_ids"] = selected_ids
        if channel_info is not None:
            metadata["channels"] = channel_info
        if scores is not None:
            metadata["scores"] = scores

        return await trace_service.create_span(
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            user_id=user_id,
            session_id=session_id,
            step_type="memory_retrieve",
            step_name="retrieve_personal_memories",
            span_id=span_id,
            input_event_ids_json=json.dumps(
                [query_analysis.get("input_event_id")] if query_analysis else [],
                ensure_ascii=False,
            ),
            output_memory_ids_json=json.dumps(selected_ids or [], ensure_ascii=False),
            decision="select_top_memories",
            decision_reason="根据精确 Key、FTS、向量相关性、时间和重要性选择",
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )


# ── 内部帮助类 ──────────────────────────────────────────────────


class _Candidate:
    def __init__(self, memory: dict[str, Any]) -> None:
        self.memory = memory
        self.ranks: list[int] = []
        self.channels: list[str] = []
        self.final_score: float = 0.0


def _add_candidate(pool: dict[str, _Candidate], memory: dict[str, Any], channel: str, rank: int) -> None:
    mem_id = memory["id"]
    if mem_id not in pool:
        pool[mem_id] = _Candidate(memory)
    pool[mem_id].ranks.append(rank)
    if channel not in pool[mem_id].channels:
        pool[mem_id].channels.append(channel)
