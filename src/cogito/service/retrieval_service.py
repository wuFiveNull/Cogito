"""RetrievalService — 共享检索接口。

E3+E4+E5: 统一 ContextBuilder 隐式召回和 recall_memory 显式检索的检索路径。
- 硬过滤: principal / scope / status=confirmed / deleted_at / valid_to / supersedes
- 软评分: keyword / scope / importance / confidence / recency / explicitness
- FTS5: 中文 trigram 优先，不可用时 unicode61 + LIKE 降级
- 所有分项归一化到 [0,1]，策略版本写入 ContextSnapshot
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from cogito.domain.memory import MemoryItem, MemoryKind, MemoryStatus
from cogito.store.memory_repo import MemoryRepository, _row_to_memory

_LOGGER = logging.getLogger(__name__)

# ── 评分系数 ──
KEYWORD_WEIGHT = 0.25
SEMANTIC_WEIGHT = 0.20
SCOPE_WEIGHT = 0.15
IMPORTANCE_WEIGHT = 0.15
CONFIDENCE_WEIGHT = 0.10
RECENCY_WEIGHT = 0.10
EXPLICITNESS_WEIGHT = 0.05

# 显式性得分映射
_EXPLICITNESS_SCORE: dict[str, float] = {
    "explicit_user_statement": 1.0,
    "confirmed_inference": 0.9,
    "external_source": 0.7,
    "system_generated": 0.6,
    "model_inference": 0.4,
}


@dataclass
class ScoredMemory:
    """带评分的记忆条目。"""
    item: MemoryItem
    score: float
    retrieval_path: str = "fts"  # "fts" | "like" | "fallback" | "list"
    keyword_hit: bool = False
    scope_match: bool = False
    semantic_similarity: float = 0.0
    source: str = "memory"
    conversation_id: str = ""
    session_id: str = ""
    principal_id: str = ""

    def to_dict(self) -> dict:
        return {
            "memory_id": self.item.memory_id,
            "kind": str(self.item.kind),
            "subject": self.item.subject,
            "predicate": self.item.predicate,
            "value": self.item.value,
            "score": round(self.score, 4),
            "retrieval_path": self.retrieval_path,
            "principal_id": self.item.principal_id,
            "scope_type": self.item.scope_type,
            "scope_id": self.item.scope_id,
        }


class RetrievalService:
    """统一检索服务。

    检索路径：
    1. 尝试 FTS5 MATCH（trigram 中文分词）
    2. 退化到 unicode61 + LIKE
    3. 语义检索（Embedding 空间，如可用）
    """

    POLICY_VERSION = "2"  # 评分策略版本（与旧版 1 区分）

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._repo = MemoryRepository(conn)
        self._fts_available: bool | None = None
        self._fts_tokenizer: str = "unicode61"

    # ── FTS5 能力检测（E4）──

    def _detect_fts_capabilities(self) -> tuple[bool, str]:
        """检测 FTS5 能力和最佳 tokenizer。

        Returns:
            (是否可用, tokenizer 名称)
        """
        if self._fts_available is not None:
            return self._fts_available, self._fts_tokenizer

        try:
            self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
                               "USING fts5("
                               "  memory_id UNINDEXED,"
                               "  subject, predicate, value,"
                               "  tokenize='unicode61'")
            self._fts_available = True

            # 尝试 trigram tokenizer（中文支持更好）
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_trigram "
                    "USING fts5(subject, predicate, value, tokenize='trigram')"
                )
                self._conn.execute("DROP TABLE IF EXISTS memory_fts_trigram")
                self._fts_tokenizer = "trigram"
            except sqlite3.OperationalError:
                self._fts_tokenizer = "unicode61"

            self._fts_rebuild()
        except sqlite3.OperationalError:
            self._fts_available = False
            self._fts_tokenizer = "none"

        return self._fts_available, self._fts_tokenizer

    def _fts_rebuild(self) -> None:
        """从 memory_items 全量重建 FTS 索引（幂等）。"""
        if not self._fts_available:
            return
        try:
            self._conn.execute("DELETE FROM memory_fts")
            self._conn.execute(
                "INSERT INTO memory_fts (memory_id, subject, predicate, value) "
                "SELECT memory_id, subject, predicate, value FROM memory_items "
                "WHERE deleted_at IS NULL AND status IN ('confirmed', 'candidate')"
                " AND (valid_to IS NULL OR valid_to > ?)",
                (datetime.now(UTC).isoformat(),),
            )
        except sqlite3.OperationalError:
            pass

    def _sync_fts_insert(self, memory_id: str, subject: str, predicate: str, value: str) -> None:
        """同步插入 FTS 索引。"""
        if not self._fts_available:
            return
        try:
            self._conn.execute(
                "INSERT INTO memory_fts (memory_id, subject, predicate, value) "
                "VALUES (?, ?, ?, ?)",
                (memory_id, subject, predicate, value),
            )
        except sqlite3.OperationalError:
            pass

    def _sync_fts_delete(self, memory_id: str) -> None:
        """同步删除 FTS 索引。"""
        if not self._fts_available:
            return
        try:
            self._conn.execute(
                "DELETE FROM memory_fts WHERE memory_id=?", (memory_id,),
            )
        except sqlite3.OperationalError:
            pass

    # ── 核心检索接口 ──

    def retrieve(
        self,
        principal_id: str,
        query: str = "",
        scope_type: str = "",
        scope_id: str = "",
        kinds: list[str] | None = None,
        limit: int = 20,
        include_embeddings: bool = False,
    ) -> list[ScoredMemory]:
        """检索有效记忆（E5: 硬过滤 + 软评分）。"""
        scored = self._search_scored(
            principal_id=principal_id,
            query=query,
            scope_type=scope_type,
            scope_id=scope_id,
            kinds=kinds,
            limit=limit,
        )
        return scored

    def retrieve_for_context(
        self,
        principal_id: str,
        query: str = "",
        session_id: str = "",
        conversation_id: str = "",
        limit: int = 50,
        memory_budget_tokens: int = 2000,
    ) -> tuple[list[ScoredMemory], list[str]]:
        """ContextBuilder 使用的隐式召回接口。

        Returns:
            (评分结果列表, 注入的记忆 ID 列表)
        """
        if not principal_id:
            return [], []

        scope_type = "" if session_id else "global"
        scope_id = session_id or conversation_id

        scored = self._search_scored(
            principal_id=principal_id,
            query=query,
            scope_type=scope_type,
            scope_id=scope_id,
            kinds=None,
            limit=limit,
        )

        # Apply budget
        budget_used = 0
        results: list[ScoredMemory] = []
        memory_ids: list[str] = []
        for sm in scored:
            entry_tokens = _estimate_entry_tokens(sm.item)
            if budget_used + entry_tokens > memory_budget_tokens:
                continue
            results.append(sm)
            memory_ids.append(sm.item.memory_id)
            budget_used += entry_tokens

        return results, memory_ids

    # ── 内部搜索逻辑 ──

    def _search_scored(
        self,
        principal_id: str,
        query: str = "",
        scope_type: str = "",
        scope_id: str = "",
        kinds: list[str] | None = None,
        limit: int = 10,
    ) -> list[ScoredMemory]:
        """按文本搜索有效记忆（E4: FTS5 可用时使用 BM25 + LIKE 降级）。

        降级链（Plan 02 M6）：
        1. FTS5 MATCH（BM25）→ 命中即返回
        2. FTS 损坏/无命中 → LIKE 降级
        3. 两者都无结果 + query 非空 → recency fallback（近期高重要性记忆）
        """
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        # ── 硬过滤条件 ──
        conditions = [
            "mi.status='confirmed'",
            "mi.deleted_at IS NULL",
            "(mi.valid_to IS NULL OR mi.valid_to > ?)",
            "mi.principal_id=?",
        ]
        params: list[Any] = [now_iso, principal_id]

        if scope_type:
            conditions.append("mi.scope_type=?")
            params.append(scope_type)
        if scope_id:
            conditions.append("mi.scope_id=?")
            params.append(scope_id)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            conditions.append(f"mi.kind IN ({placeholders})")
            params.extend(kinds)

        # 排除 supersession 退出历史的记忆
        conditions.append(
            "mi.memory_id NOT IN ("
            "  SELECT supersedes_id FROM memory_items "
            "  WHERE supersedes_id IS NOT NULL AND deleted_at IS NULL"
            ")"
        )

        fts_ok, _ = self._detect_fts_capabilities()
        has_query = bool(query)

        # ── (1) FTS5 路径 ──
        if fts_ok and has_query:
            fts_expr = _fts_escape(query)
            try:
                fts_where = " AND ".join(conditions) if conditions else "1=1"
                rows = self._conn.execute(
                    "SELECT mi.* FROM memory_items mi "
                    "WHERE mi.memory_id IN ("
                    "  SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?"
                    ") AND " + fts_where,
                    [fts_expr] + params,
                ).fetchall()
                if rows:
                    return self._build_results(
                        rows, query=query, scope_type=scope_type,
                        scope_id=scope_id, keyword_hit=True, now=now,
                        retrieval_path="fts",
                        limit=limit,
                    )
            except sqlite3.OperationalError:
                pass

        # ── (2) LIKE 降级路径 ──
        if has_query:
            like_pattern = f"%{query}%"
            like_conditions = conditions + [
                "(mi.value LIKE ? OR mi.subject LIKE ? OR mi.predicate LIKE ?)"
            ]
            like_params = params + [like_pattern, like_pattern, like_pattern]

            sql = ("SELECT mi.* FROM memory_items mi WHERE "
                   + " AND ".join(like_conditions)
                   + " ORDER BY mi.importance DESC, mi.confidence DESC, mi.created_at DESC"
                   + f" LIMIT {int(limit)}")

            rows = self._conn.execute(sql, like_params).fetchall()
            if rows:
                return self._build_results(
                    rows, query=query, scope_type=scope_type,
                    scope_id=scope_id, keyword_hit=True, now=now,
                    retrieval_path="like",
                    limit=limit,
                )

        # ── (3) recency fallback：无 query 匹配 → 返回近期高重要性记忆 ──
        if not has_query or True:  # list 模式始终走此路径
            sql = ("SELECT mi.* FROM memory_items mi WHERE "
                   + " AND ".join(conditions)
                   + " ORDER BY mi.importance DESC, mi.confidence DESC, mi.created_at DESC"
                   + f" LIMIT {int(limit)}")
            rows = self._conn.execute(sql, params).fetchall()
            return self._build_results(
                rows, query=query, scope_type=scope_type,
                scope_id=scope_id, keyword_hit=False, now=now,
                retrieval_path="list",
                limit=limit,
            )

    def _build_results(
        self,
        rows: list[sqlite3.Row],
        query: str,
        scope_type: str,
        scope_id: str,
        keyword_hit: bool,
        now: datetime,
        retrieval_path: str,
        limit: int,
    ) -> list[ScoredMemory]:
        """构建带评分的结果列表。"""
        results: list[ScoredMemory] = []
        for r in rows:
            item = _row_to_memory(r)
            scope_match = (
                (not scope_type or item.scope_type == scope_type)
                and (not scope_id or item.scope_id == scope_id)
            )
            score = _compute_weighted_score(
                item, keyword_hit=keyword_hit, scope_match=scope_match,
                semantic_similarity=0.0, now=now,
            )
            results.append(ScoredMemory(
                item=item,
                score=score,
                retrieval_path=retrieval_path,
                keyword_hit=keyword_hit,
                scope_match=scope_match,
                principal_id=item.principal_id,
                conversation_id=getattr(item, "conversation_id", "") or "",
                session_id="",
            ))

        results.sort(key=lambda sm: -sm.score)
        return results[:limit]


def _compute_weighted_score(
    item: MemoryItem,
    keyword_hit: bool = False,
    scope_match: bool = False,
    semantic_similarity: float = 0.0,
    now: datetime | None = None,
) -> float:
    """计算记忆的加权检索评分（E5: 各项归一化到 [0,1]）。"""
    if now is None:
        now = datetime.now(UTC)

    kw_score = 1.0 if keyword_hit else 0.0
    sem_score = max(0.0, min(1.0, semantic_similarity))
    sc_score = 1.0 if scope_match else 0.0
    imp_score = item.importance
    conf_score = item.confidence

    age_days = (item.created_at - now).total_seconds() / 86400 if item.created_at else 365
    recency_score = max(0.0, 1.0 - abs(age_days) / 365.0)
    expl_score = _EXPLICITNESS_SCORE.get(item.explicitness, 0.5)

    return (
        KEYWORD_WEIGHT * kw_score
        + SEMANTIC_WEIGHT * sem_score
        + SCOPE_WEIGHT * sc_score
        + IMPORTANCE_WEIGHT * imp_score
        + CONFIDENCE_WEIGHT * conf_score
        + RECENCY_WEIGHT * recency_score
        + EXPLICITNESS_WEIGHT * expl_score
    )


def _estimate_entry_tokens(item: MemoryItem) -> int:
    """估算单个记忆条目在上下文中占用的 token 数。"""
    text = f"- [{str(item.kind)}] {item.subject}/{item.predicate} = {item.value}"
    return max(1, len(text) // 4)


def _fts_escape(query: str) -> str:
    """转义 FTS5 特殊字符，构建安全的多词查询（E4: 中文处理）。"""
    if not query:
        return ""
    tokens = re.findall(r"[-\w一-鿿㐀-䶿]+", query, re.UNICODE)
    if not tokens:
        return query
    if len(tokens) == 1:
        return tokens[0]
    return " OR ".join(tokens)
