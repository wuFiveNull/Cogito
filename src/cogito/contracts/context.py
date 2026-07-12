"""Context Snapshot + ContextBuilder — 不可变上下文装配 (PLAN-09 M3).

Pure layer (no infra imports): depends only on
- `cogito.domain` (Message / ContentPart / MemoryItem)
- `cogito.contracts` (Clock / MemoryReader)

Previous location: `cogito.runtime.context` (kept as re-export shim).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from cogito.contracts.budget import TokenBudgetConfig
from cogito.contracts.clock import Clock, ProductionClock, epoch_ms
from cogito.contracts.memory import MemoryReader
from cogito.contracts.multimodal import MultimodalContextReader
from cogito.contracts.retrieval import RetrievalCandidate

# 简单 Token 估算器：每字符约 0.25 token
_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    """字符级 Token 估算。"""
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


# ── PLAN-16 M6 RET-02/04: query-aware scoring helpers ──────────────────────

# RETRIEVAL-CONTEXT §6.1 综合分数分量权重（memory 无 embedding，semantic=0）。
# keyword + recency + importance + confidence + trust；权重和 = 1.0。
_KW_WEIGHT = 0.25
_RECENCY_WEIGHT = 0.10
_IMPORTANCE_WEIGHT = 0.35
_CONFIDENCE_WEIGHT = 0.15
_TRUST_WEIGHT = 0.15
_RECENCY_HALF_LIFE_DAYS = 30.0


def _tokenize(text: str) -> list[str]:
    """简单的 lower-case 分词（中英文），用于 keyword 重叠度计算。"""
    return re.findall(r"[一-鿿㐀-䶿]+|[A-Za-z0-9]+", text.lower())


def keyword_score(query: str, subject: str, predicate: str, value: str) -> float:
    """query 与记忆文本的 keyword 重叠度 ∈ [0,1]（PLAN-16 M6 RET-04）。

    MemoryRetriever 接收当前 query 后（RET-02），用 query terms 在记忆文本上的
    覆盖比例作为 keyword 分量；query 为空返回 0（保持原行为）。
    """
    query_terms = [t for t in _tokenize(query) if len(t) > 0]
    if not query_terms:
        return 0.0
    blob_terms = set(_tokenize(f"{subject} {predicate} {value}"))
    if not blob_terms:
        return 0.0
    hits = sum(1 for qt in query_terms if any(qt in bt for bt in blob_terms))
    return min(1.0, hits / len(query_terms))


def recency_score(created_at: datetime | None) -> float:
    """时间衰减得分 ∈ [0,1]，半衰期 _RECENCY_HALF_LIFE_DAYS。"""
    if created_at is None:
        return 0.0
    try:
        age_days = abs((datetime.now(UTC) - created_at).total_seconds()) / 86400.0
    except (TypeError, ValueError):
        return 0.0
    # 指数衰减：0 天=1.0，半衰期后=0.5。
    import math
    return float(math.exp(-0.6931 * age_days / _RECENCY_HALF_LIFE_DAYS))


def normalize_scores(candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
    """按 candidate_type 组内 min-max 标准化 final_score（PLAN-16 M6 RET-04）。

    不同来源（memory / knowledge_segment / ...）的原始 score 量纲不同，
    直接比较会被高基来源主导。组内标准化后 final_score ∈ [0,1]，可跨源比较。
    仅有一条候选的组保持原值（无法标准化）。
    """
    if not candidates:
        return candidates
    by_type: dict[str, list[tuple[int, float]]] = {}
    for idx, c in enumerate(candidates):
        by_type.setdefault(c.candidate_type, []).append((idx, c.final_score))
    new_scores: dict[int, float] = {}
    for group in by_type.values():
        if len(group) <= 1:
            if group:
                new_scores[group[0][0]] = group[0][1]
            continue
        vals = [s for _, s in group]
        lo, hi = min(vals), max(vals)
        span = hi - lo
        for idx, s in group:
            new_scores[idx] = (s - lo) / span if span > 0 else 0.0
    if not new_scores:
        return candidates
    out = []
    for idx, c in enumerate(candidates):
        ns = new_scores.get(idx)
        if ns is None or ns == c.final_score:
            out.append(c)
        else:
            out.append(RetrievalCandidate(
                candidate_type=c.candidate_type,
                candidate_id=c.candidate_id,
                principal_id=c.principal_id,
                scope=c.scope,
                content_ref=c.content_ref,
                source_refs=c.source_refs,
                keyword_score=c.keyword_score,
                semantic_score=c.semantic_score,
                recency_score=c.recency_score,
                importance_score=c.importance_score,
                trust_score=c.trust_score,
                final_score=ns,
                token_estimate=c.token_estimate,
                retrieval_path=c.retrieval_path,
                policy_version=c.policy_version,
                exclusion_reason=c.exclusion_reason,
            ))
    return out


@dataclass(frozen=True)
class ContextItem:
    """Snapshot 中的单个上下文条目。

    每条 item 保留 source/score/tokens/trust_label/retrieval_path (Plan 02 M5)。
    """
    item_type: str  # "message" | "system_policy" | "memory" | "summary" | "knowledge"
    item_id: str
    source: str  # session_id 或 "system"
    tokens: int = 0
    trust_label: str = "unverified"
    content: str = ""
    role: str = ""  # "user" | "assistant" | "tool" | "system"
    score: float = 0.0  # 相关性/重要性分数（用于可解释性）
    retrieval_path: str = ""  # 命中路径: "keyword" | "vector" | "keyword+vector"
    # PLAN-13 P13-12：来源版本、score 分项、retrieval path、policy version（不可变快照来源解释）
    provenance: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ContextSnapshot:
    """不可变上下文快照。

    - snapshot_id: 稳定标识
    - turn_id: 关联的 Turn
    - input_message_id: 当前输入消息 ID
    - session_id: 来源 Session
    - principal_id: 来源 Principal
    - message_upper_bound: 创建时的消息上界
    - selection_policy_version: 选择策略版本
    - items: 选中的上下文条目
    - memory_ids: 注入的记忆 ID 列表
    - excluded_summary: 被裁剪的内容摘要说明
    - total_tokens: 条目总 Token 数
    - created_at: 创建时间
    """
    snapshot_id: str = ""
    turn_id: str = ""
    input_message_id: str = ""
    session_id: str = ""
    conversation_id: str = ""
    principal_id: str = ""
    message_upper_bound: int = 0
    query_plan_version: str = "1"  # Query Plan 版本（Plan 02 M5）
    selection_policy_version: str = "1"
    items: tuple[ContextItem, ...] = ()
    memory_ids: tuple[str, ...] = ()
    excluded_summary: str = ""
    total_tokens: int = 0
    created_at: int = 0
    # PLAN-13 P13-12：各源实际 Token 分配（per-source budget 可解释性）
    per_source_tokens: tuple[tuple[str, int], ...] = ()
    # PLAN-13 P13-12：排除摘要统计（unauthorized/stale/superseded/low score/token budget/duplicate）
    exclusion_stats: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "memory_ids", tuple(self.memory_ids))
        object.__setattr__(self, "per_source_tokens", tuple(self.per_source_tokens))
        object.__setattr__(self, "exclusion_stats", tuple(self.exclusion_stats))


class KnowledgeReader(Protocol):
    def retrieve(
        self, *, principal_id: str, query: str, limit: int = 8,
        query_vector: list[float] | None = None,
    ) -> list[dict]: ...


class ContextBuilder:
    """构建不可变 ContextSnapshot。

    MVP 规则：
    - 只读取当前 session_id
    - 当前输入必选
    - 近期消息按持久 receive_sequence 排序
    - 使用稳定字符估算器预留输出预算
    - 超限时从最旧的普通历史消息开始裁剪
    - System Policy 和当前输入不得裁剪
    - 不读取跨 Session 历史
    - 所有外部内容保留 Trust Label
    """

    # ── 上下文压缩常量（阶段 6）──
    SOFT_THRESHOLD = 0.65
    BACKGROUND_THRESHOLD = 0.75
    HARD_THRESHOLD = 0.85
    EMERGENCY_THRESHOLD = 0.95
    KEEP_RECENT_COUNT = 10
    KEEP_RECENT_TOKENS = 2000
    # PLAN-13/R-12：各源预算统一由 TokenBudgetConfig 管理；此处仅保留兜底默认值。
    _MEMORY_MAX_ITEMS = 50
    _KIND_PRIORITY: dict[str, int] = {
        "constraint": 0,
        "preference": 1,
        "goal": 2,
        "fact": 3,
        "episode": 4,
    }

    def __init__(
        self,
        conn,  # sqlite3.Connection — 保持与原有签名兼容
        clock: Clock | None = None,
        max_input_tokens: int = 64000,
        policy_version: str = "1",
        query_plan_version: str = "1",
        memory_reader: MemoryReader | None = None,
        multimodal_reader: MultimodalContextReader | None = None,
        knowledge_reader: KnowledgeReader | None = None,
        knowledge_top_k: int = 8,
        # PLAN-13/R-12: knowledge_budget_ratio 保留为兼容参数；
        # 实际配额由 _budget_config.knowledge_segments_ratio 决定。
        knowledge_budget_ratio: float = 0.20,
        budget_config: TokenBudgetConfig | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or ProductionClock()
        self._max_input_tokens = max_input_tokens
        self._policy_version = policy_version
        self._query_plan_version = query_plan_version
        self._memory_reader = memory_reader
        self._multimodal_reader = multimodal_reader
        self._knowledge_reader = knowledge_reader
        self._knowledge_top_k = knowledge_top_k
        self._knowledge_budget_ratio = knowledge_budget_ratio
        # PLAN-13/R-12：统一 budget 配置（默认使用 PLAN-13 §13.4 推荐值）
        self._budget_config = budget_config or TokenBudgetConfig(
            knowledge_segments_ratio=knowledge_budget_ratio,
        )

    def build(
        self,
        turn_id: str,
        session_id: str,
        input_message_id: str,
        system_policy: str = "",
    ) -> ContextSnapshot:
        """构建不可变 ContextSnapshot。

        装配顺序（RETRIEVAL-CONTEXT / 10）：
        system policy → memory → summary → 历史消息 → 当前用户输入
        """

        messages = self._load_session_messages(session_id)
        input_seq = self._find_input_sequence(input_message_id)
        message_upper_bound = max((m["sequence"] for m in messages), default=0)

        input_msg = None
        history: list[dict] = []
        for msg in messages:
            if msg["sequence"] == input_seq:
                input_msg = msg
            else:
                history.append(msg)

        principal_id = (input_msg or {}).get("sender_principal_id", "") or ""

        items: list[ContextItem] = []

        # 1. System Policy 必选（最前）
        if system_policy:
            items.append(ContextItem(
                item_type="system_policy",
                item_id="system_policy",
                source="system",
                tokens=estimate_tokens(system_policy),
                trust_label="internal",
                content=system_policy,
                role="system",
            ))

        # 2. 注入长期记忆（阶段 3）
        # RET-02: query = 当前用户输入内容，驱动记忆 query-aware 相关性召回
        query_text = (input_msg or {}).get("content", "") or ""
        injection = self._inject_memories(
            principal_id, session_id, query=query_text,
        )
        memory_items, memory_ids, memory_excluded = injection
        items.extend(memory_items)
        # RET-05: 记忆源排除原因暂存，最终汇入 Snapshot exclusion_stats
        self._memory_excluded: dict[str, str] = memory_excluded

        # 2b. Authorized content memory.  It is an independent source and can be
        # disabled without changing the existing fact-memory path.
        if input_msg and self._knowledge_reader is not None:
            items.extend(self._inject_knowledge(
                principal_id, input_msg.get("content", ""),
            ))

        # 3. 上下文压缩（阶段 6）
        history_itemized = [self._message_to_item(m) for m in history]
        input_itemized = self._message_to_item(input_msg) if input_msg else None

        base_tokens = sum(i.tokens for i in items)
        history_tokens = sum(i.tokens for i in history_itemized)
        input_tokens = input_itemized.tokens if input_itemized else 0
        total_estimate = base_tokens + history_tokens + input_tokens
        token_ratio = (
            total_estimate / self._max_input_tokens
            if self._max_input_tokens > 0 else 0
        )

        if token_ratio >= self.BACKGROUND_THRESHOLD:
            summary = self._load_active_summary(session_id)
            if summary:
                covers_to = summary["covers_to_seq"]
                keep_min = min(self.KEEP_RECENT_COUNT, len(history))
                cutoff_idx = len(history) - keep_min
                old_count = 0
                recent: list[dict] = []
                for i, msg in enumerate(history):
                    if i < cutoff_idx and msg.get("sequence", 0) <= covers_to:
                        old_count += 1
                    else:
                        recent.append(msg)

                if old_count > 0:
                    summary_content = self._format_summary(summary["content_json"])
                    items.append(ContextItem(
                        item_type="summary",
                        item_id=summary["summary_id"],
                        source=session_id,
                        tokens=estimate_tokens(summary_content),
                        trust_label="verified",
                        content=summary_content,
                        role="system",
                    ))
                    history = recent

        # 4. 历史消息（时间正序）
        for msg in history:
            items.append(self._message_to_item(msg))

        # 5. 当前用户输入（最后）
        if input_msg:
            items.append(self._message_to_item(input_msg))

        # 6. Token 超限裁剪
        total_tokens = sum(i.tokens for i in items)
        excluded: list[str] = []

        if total_tokens > self._max_input_tokens:
            clipped_items, excluded = self._clip_to_budget(items, input_message_id)
            items = clipped_items
            total_tokens = sum(i.tokens for i in items)

        per_source: dict[str, int] = {}
        for item in items:
            per_source[item.item_type] = per_source.get(item.item_type, 0) + item.tokens
        exclusion_counts: dict[str, int] = {}
        for value in excluded:
            kind = value.split(":", 1)[0] if ":" in value else "token_budget"
            exclusion_counts[kind] = exclusion_counts.get(kind, 0) + 1
        # RET-05: 合并记忆源选择阶段的排除原因（token_budget 等）到 Snapshot
        for reason in getattr(self, "_memory_excluded", {}).values():
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1

        snapshot = ContextSnapshot(
            snapshot_id=uuid.uuid4().hex,
            turn_id=turn_id,
            input_message_id=input_message_id,
            session_id=session_id,
            conversation_id=self._get_session_conversation(session_id),
            principal_id=principal_id,
            memory_ids=tuple(memory_ids),
            message_upper_bound=message_upper_bound,
            query_plan_version=self._query_plan_version,
            selection_policy_version=self._policy_version,
            items=tuple(items),
            excluded_summary=(
                f"Excluded {len(excluded)} items: {', '.join(excluded[:10])}"
                if excluded else ""
            ),
            total_tokens=total_tokens,
            created_at=epoch_ms(self._clock.now()),
            per_source_tokens=tuple(sorted(per_source.items())),
            exclusion_stats=tuple(sorted(exclusion_counts.items())),
        )

        return snapshot

    def _build_knowledge_candidates(
        self, results: list[dict],
    ) -> list[RetrievalCandidate]:
        """PLAN-13/R-12: 将 KnowledgeReader 返回的 dict 映射为 RetrievalCandidate。"""
        candidates: list[RetrievalCandidate] = []
        for value in results:
            content = str(value.get("text_ref_or_inline", ""))
            if not content:
                continue
            segment_id = str(value.get("segment_id", ""))
            resource_id = str(value.get("resource_id", ""))
            score = float(value.get("score", 0.0))
            retrieval_path = str(value.get("retrieval_path", "keyword"))
            candidates.append(RetrievalCandidate(
                candidate_type="knowledge_segment",
                candidate_id=segment_id,
                principal_id=str(value.get("principal_id", "")),
                scope="",
                content_ref=content,
                token_estimate=int(value.get("token_count") or estimate_tokens(content)),
                keyword_score=score if "keyword" in retrieval_path else 0.0,
                semantic_score=score if "vector" in retrieval_path else 0.0,
                recency_score=0.0,
                importance_score=0.0,
                trust_score=1.0 if value.get("trust_label") in ("internal", "verified") else 0.7,
                final_score=score,
                retrieval_path=retrieval_path,
                policy_version=self._policy_version,
            ))
        return candidates

    def _inject_knowledge(self, principal_id: str, query: str) -> list[ContextItem]:
        """PLAN-13/R-12: 构建知识候选 → 按 knowledge 源预算选择 → ContextItem。"""
        if not principal_id or not query.strip() or self._knowledge_reader is None:
            return []
        try:
            results = self._knowledge_reader.retrieve(
                principal_id=principal_id, query=query, limit=self._knowledge_top_k,
            )
        except Exception:
            return []

        candidates = self._build_knowledge_candidates(results)

        # PLAN-16 M6 RET-04: 组内标准化，使 knowledge 分数可与其他源比较
        candidates = normalize_scores(candidates)

        # PLAN-13/R-12: 按 knowledge_segment 源预算配额选择
        knowledge_budget = self._budget_config.quota(
            "knowledge_segment", max(1, self._max_input_tokens),
        )
        selected = self._select_candidates_by_budget(candidates, knowledge_budget)

        output: list[ContextItem] = []
        for c in selected:
            resource_id = ""
            # 反查 resource_id（通过 content 匹配原始 dict）
            for r in results:
                if str(r.get("segment_id", "")) == c.candidate_id:
                    resource_id = str(r.get("resource_id", ""))
                    break
            output.append(ContextItem(
                item_type="knowledge",
                item_id=c.candidate_id,
                source=resource_id,
                tokens=max(1, c.token_estimate),
                trust_label="verified" if c.trust_score >= 1.0 else "unverified",
                content=f"<knowledge_segment>\n{c.content_ref}\n</knowledge_segment>",
                role="system",
                score=c.final_score,
                retrieval_path=c.retrieval_path,
                provenance=(
                    ("resource_id", resource_id),
                    ("segment_id", c.candidate_id),
                    ("keyword_score", f"{c.keyword_score:.3f}"),
                    ("semantic_score", f"{c.semantic_score:.3f}"),
                    ("final_score", f"{c.final_score:.3f}"),
                    ("retrieval_path", c.retrieval_path),
                    ("token_estimate", str(c.token_estimate)),
                    ("policy_version", self._policy_version),
                    ("query_plan_version", self._query_plan_version),
                ),
            ))
        return output

    # ── 内部方法（与原始 runtime/context.py 完全一致）──

    def _get_session_conversation(self, session_id: str) -> str:
        row = self._conn.execute(
            "SELECT conversation_id FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return row["conversation_id"] if row else ""

    def _load_active_summary(self, session_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT summary_id, covers_to_seq, content_json "
            "FROM session_summaries "
            "WHERE session_id=? AND status='active' "
            "ORDER BY summary_version DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "summary_id": row["summary_id"],
            "covers_to_seq": row["covers_to_seq"],
            "content_json": row["content_json"],
        }

    @staticmethod
    def _format_summary(content_json: str) -> str:
        try:
            data = json.loads(content_json)
        except (json.JSONDecodeError, TypeError):
            return f"Session Summary: {content_json[:500]}"

        if isinstance(data, dict):
            lines = ["## Session Summary"]
            for key in ("conversation_goal", "summary", "user_intent", "current_state"):
                val = data.get(key)
                if val:
                    label = key.replace("_", " ").title()
                    lines.append(f"{label}: {val}")
            for key in ("confirmed_facts", "decisions", "constraints", "completed_work"):
                val = data.get(key)
                if val and isinstance(val, list):
                    for item in val:
                        lines.append(f"- {item}")
            return "\n".join(lines)
        return f"Session Summary: {content_json[:500]}"

    def _build_memory_candidates(
        self,
        memories: list,
        session_id: str,
        conversation_id: str,
        query: str = "",
    ) -> tuple[list[RetrievalCandidate], set[int]]:
        """PLAN-13/R-12 + PLAN-16 M6 RET-02/03/04: 将 MemoryItem 映射为 RetrievalCandidate。

        RET-02：MemoryRetriever 接收当前 query；query 非空时 keyword 分量按
        query terms 在记忆文本上的覆盖比例注入。
        RET-03：返回 protected_indices —— active goal（kind=goal, goal_status=active）
        与 constraint 候选（RETRIEVAL-CONTEXT §4.1），在选择时不被 budget 挤出。
        RET-04：final_score 复用 RETRIEVAL-CONTEXT §6.1 加权公式
        (keyword + recency + importance + confidence + trust); query 为空时
        退化为原 importance/confidence/trust 排序，保持现有行为不变。
        """
        session_mems: list = []
        conv_mems: list = []
        global_mems: list = []
        for m in memories:
            if m.scope_type == "session":
                if m.scope_id == session_id:
                    session_mems.append(m)
            elif m.scope_type == "conversation":
                if m.scope_id == conversation_id:
                    conv_mems.append(m)
            elif m.scope_type in ("", "global", "user"):
                global_mems.append(m)

        seen_keys: set[str] = set()
        merged: list = []
        for pool in [session_mems, conv_mems, global_mems]:
            for m in pool:
                if m.canonical_key and m.canonical_key in seen_keys:
                    continue
                if m.canonical_key:
                    seen_keys.add(m.canonical_key)
                merged.append(m)

        use_query = query and query.strip()

        def _sort_key(m):
            kind_order = self._KIND_PRIORITY.get(str(m.kind), 5)
            return (kind_order, -m.importance, -m.confidence)

        merged.sort(key=_sort_key)

        candidates: list[RetrievalCandidate] = []
        protected_indices: set[int] = set()
        for idx, m in enumerate(merged):
            kind_label = str(m.kind)
            is_explicit = m.explicitness in (
                "explicit_user_statement", "confirmed_inference",
            )
            kind_source = "goal" if kind_label in ("goal", "constraint") else "memory"
            entry_text = (
                f"- [{kind_label}, "
                f"{'explicit' if is_explicit else 'inferred'}, "
                f"confidence={m.confidence:.1f}] "
                f"{m.subject}/{m.predicate} = {m.value}"
            )
            # RET-02/04: query-aware 评分（query 为空时 keyword=0、behavior 不变）
            kw = keyword_score(query, m.subject, m.predicate, m.value) if use_query else 0.0
            rec = recency_score(m.created_at) if use_query else 0.0
            trust = 1.0 if is_explicit else 0.7
            if use_query:
                final = (
                    _KW_WEIGHT * kw
                    + _RECENCY_WEIGHT * rec
                    + _IMPORTANCE_WEIGHT * m.importance
                    + _CONFIDENCE_WEIGHT * m.confidence
                    + _TRUST_WEIGHT * trust
                )
            else:
                final = self._memory_candidate_score(m)
            candidates.append(RetrievalCandidate(
                candidate_type="memory",
                candidate_id=m.memory_id,
                principal_id=m.principal_id,
                scope=m.scope_type,
                content_ref=entry_text,
                token_estimate=estimate_tokens(entry_text),
                keyword_score=kw,
                semantic_score=0.0,
                recency_score=rec,
                importance_score=m.importance,
                trust_score=trust,
                final_score=final,
                retrieval_path="list",
                policy_version=self._policy_version,
            ))
            # RET-03: active goal / constraint 标记为 protected
            is_active_goal = (
                kind_label == "goal"
                and getattr(m, "goal_status", None) is not None
                and str(m.goal_status) == "active"
            )
            is_constraint = kind_label == "constraint"
            if is_active_goal or is_constraint:
                protected_indices.add(idx)
        return candidates, protected_indices

    @staticmethod
    def _memory_candidate_score(m) -> float:
        """记忆候选综合分 = importance * 0.5 + confidence * 0.3 + trust * 0.2。

        query 为空时复用原公式，保持现有排序行为不变。
        """
        is_explicit = m.explicitness in (
            "explicit_user_statement", "confirmed_inference",
        )
        trust = 1.0 if is_explicit else 0.7
        return m.importance * 0.5 + m.confidence * 0.3 + trust * 0.2

    def _inject_memories(
        self,
        principal_id: str,
        session_id: str,
        query: str = "",
    ) -> tuple[list[ContextItem], list[str]]:
        """PLAN-13/R-12 + PLAN-16 M6 RET-02: 构建记忆候选 → 统一 budget 选择 → 序列化。"""
        if not principal_id or not self._memory_reader:
            return [], [], {}

        all_memories = self._memory_reader.retrieve(
            principal_id=principal_id,
            limit=self._MEMORY_MAX_ITEMS,
        )
        if not all_memories:
            return [], [], {}

        conversation_id = self._get_session_conversation(session_id)
        candidates, protected_indices = self._build_memory_candidates(
            all_memories, session_id, conversation_id, query=query,
        )

        # PLAN-16 M6 RET-04: 组内标准化，使 memory 分数可与其他源比较
        candidates = normalize_scores(candidates)

        # PLAN-16 M6 RET-03: 按 memory 源预算配额选择候选（protected 不被挤出）
        memory_budget = self._memory_budget_tokens()
        selected, excluded = self._select_candidates_by_budget(
            candidates, memory_budget, protected_indices=protected_indices,
        )

        if not selected:
            return [], [], excluded

        lines = ["<relevant_memories>"]
        memory_ids: list[str] = []
        for position, c in enumerate(selected, 1):
            lines.append(f"  {c.content_ref}")
            memory_ids.append(c.candidate_id)
        lines.append("</relevant_memories>")
        content = "\n".join(lines)
        # PLAN-16 M6 RET-05: Snapshot provenance 增加 score 分项、选择位置与排除原因
        selected_score = max(c.final_score for c in selected) if selected else 0.0
        provenance = (
            ("source_count", str(len(selected))),
            ("pool_size", str(len(candidates))),
            ("protected_count", str(len(protected_indices))),
            ("excluded_count", str(len(excluded))),
            ("top_score", f"{selected_score:.3f}"),
            ("avg_score", f"{(sum(c.final_score for c in selected) / len(selected)):.3f}"
             if selected else "0.0"),
            ("retrieval_path", "list"),
            ("policy_version", self._policy_version),
            ("query_plan_version", self._query_plan_version),
        )
        item = ContextItem(
            item_type="memory",
            item_id="_injected_memory",
            source="memory",
            tokens=estimate_tokens(content),
            trust_label="verified",
            content=content,
            role="system",
            score=selected_score,
            retrieval_path="list",
            provenance=provenance,
        )
        return [item], memory_ids, excluded

    def _memory_budget_tokens(self) -> int:
        """PLAN-13/R-12: 计算记忆源可用 token 配额。

        使用 TokenBudgetConfig.fact_memory_ratio 计算占比；
        当 max_input_tokens 较小（如测试 1500）导致配额 ≤ 0 时，
        退化为至少允许 1 条记忆（兜底，兼容旧行为）。
        """
        usable = max(1, self._max_input_tokens)
        ratio = self._budget_config.fact_memory_ratio
        budget = int(usable * ratio)
        # 兜底：至少一条最小记忆
        return max(budget, 200)

    def _select_candidates_by_budget(
        self,
        candidates: list[RetrievalCandidate],
        budget_tokens: int,
        protected_indices: set[int] | None = None,
    ) -> tuple[list[RetrievalCandidate], dict[str, str]]:
        """按 final_score 降序选择候选，累计 token 不超 budget（PLAN-16 M6 RET-03/05）。

        RET-03：protected_indices 指向 active goal/constraint 候选，
        这些候选不被 budget 挤出（RETRIEVAL-CONTEXT §4.1/§10.4）。
        RET-05：返回 excluded mapping {candidate_id: reason}，记录每条被排除原因
        （token_budget / excluded_low_score），供 Snapshot provenance 使用。

        预算 ≤ 0 时仅返回 protected 候选。
        """
        protected_indices = protected_indices or set()
        # protected 优先按分数排；非 protected 按分数排
        order = sorted(
            range(len(candidates)),
            key=lambda i: (i not in protected_indices, -candidates[i].final_score),
        )
        selected: list[RetrievalCandidate] = []
        excluded: dict[str, str] = {}
        used = 0
        for i in order:
            c = candidates[i]
            if c.exclusion_reason:
                excluded[c.candidate_id] = c.exclusion_reason
                continue
            if i in protected_indices:
                # RET-03：protected 必选，不计入 budget 限制
                selected.append(c)
                used += max(1, c.token_estimate)
                continue
            if budget_tokens > 0 and used + max(1, c.token_estimate) > budget_tokens:
                excluded[c.candidate_id] = "token_budget"
                continue
            selected.append(c)
            used += max(1, c.token_estimate)
        return selected, excluded

    def _clip_to_budget(
        self,
        items: list[ContextItem],
        input_message_id: str,
    ) -> tuple[list[ContextItem], list[str]]:
        protected_indices: set[int] = set()
        for i, item in enumerate(items):
            if item.item_type == "system_policy":
                protected_indices.add(i)
            elif item.item_id == input_message_id:
                protected_indices.add(i)

        trimmed: list[ContextItem] = []
        excluded: list[str] = []

        running = 0
        for i, item in enumerate(items):
            if i in protected_indices:
                trimmed.append(item)
                running += item.tokens
            elif running + item.tokens <= self._max_input_tokens:
                trimmed.append(item)
                running += item.tokens
            else:
                excluded.append(f"{item.item_type}:{item.item_id}")

        return trimmed, excluded

    def _load_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT m.message_id, m.role, m.direction, m.receive_sequence, "
            "  m.trust_label, m.session_id, m.sender_principal_id, "
            "  cp.inline_data, cp.content_type, cp.ordinal "
            "FROM messages m "
            "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
            "WHERE m.session_id=? "
            "ORDER BY m.receive_sequence ASC, cp.ordinal ASC, cp.part_id ASC",
            (session_id,),
        ).fetchall()

        message_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            mid = row["message_id"]
            if mid not in message_map:
                message_map[mid] = {
                    "message_id": mid,
                    "role": row["role"],
                    "direction": row["direction"],
                    "sequence": row["receive_sequence"],
                    "trust_label": row["trust_label"],
                    "session_id": row["session_id"],
                    "sender_principal_id": row["sender_principal_id"],
                    "content_parts": [],
                }
            if (
                row["inline_data"]
                and row["content_type"] in ("text", "markdown")
            ):
                message_map[mid]["content_parts"].append(row["inline_data"])

        result = []
        for msg in message_map.values():
            content_parts = list(msg["content_parts"])
            if self._multimodal_reader is not None:
                for asset in self._multimodal_reader.list_for_message(msg["message_id"]):
                    status = asset.get("status", "queued")
                    description = asset.get("short_description", "")
                    if status == "succeeded" and description:
                        external = description
                    elif status == "failed":
                        external = "Visual analysis failed; continue using text-only context."
                    else:
                        external = "Visual analysis is pending."
                    content_parts.append(
                        "<multimodal_asset "
                        f"asset_id=\"{asset.get('asset_id', '')}\" "
                        f"mime_type=\"{asset.get('mime_type', '')}\" "
                        f"status=\"{status}\">\n"
                        "<external_data trust=\"unverified\">\n"
                        f"{external}\n"
                        "</external_data>\n"
                        "</multimodal_asset>"
                    )
            result.append({
                "message_id": msg["message_id"],
                "role": msg["role"],
                "direction": msg["direction"],
                "sequence": msg["sequence"],
                "trust_label": msg["trust_label"],
                "session_id": msg["session_id"],
                "sender_principal_id": msg.get("sender_principal_id", ""),
                "content": (
                    "\n".join(content_parts)
                    if content_parts else ""
                ),
            })
        result.sort(key=lambda m: m["sequence"])
        return result

    def _find_input_sequence(self, input_message_id: str) -> int:
        row = self._conn.execute(
            "SELECT receive_sequence FROM messages WHERE message_id=?",
            (input_message_id,),
        ).fetchone()
        return row["receive_sequence"] if row else 0

    def _message_to_item(self, msg: dict[str, Any]) -> ContextItem:
        return ContextItem(
            item_type="message",
            item_id=msg["message_id"],
            source=msg.get("session_id", ""),
            tokens=estimate_tokens(msg.get("content", "")),
            trust_label=msg.get("trust_label", "unverified"),
            content=msg.get("content", ""),
            role=msg.get("role", "user"),
        )
