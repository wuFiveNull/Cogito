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

from cogito.contracts.budget import TokenBudgetConfig, allocate_budget, select_candidates
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
            out.append(
                RetrievalCandidate(
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
                )
            )
    return out


def _candidate_provenance(candidate: RetrievalCandidate, *, reason: str = "") -> dict[str, Any]:
    """构建被排除候选的完整 provenance dict（PLAN-16 M6 #14 完整）。

    保留 score 分项 + 排除原因，供 ContextSnapshot.excluded 可解释性追溯。
    """
    return {
        "candidate_type": candidate.candidate_type,
        "candidate_id": candidate.candidate_id,
        "principal_id": candidate.principal_id,
        "scope": candidate.scope,
        "keyword_score": round(candidate.keyword_score, 4),
        "semantic_score": round(candidate.semantic_score, 4),
        "recency_score": round(candidate.recency_score, 4),
        "importance_score": round(candidate.importance_score, 4),
        "trust_score": round(candidate.trust_score, 4),
        "final_score": round(candidate.final_score, 4),
        "token_estimate": candidate.token_estimate,
        "retrieval_path": candidate.retrieval_path,
        "policy_version": candidate.policy_version,
        "exclusion_reason": reason or candidate.exclusion_reason or "",
    }


def _hard_filter(
    candidates: list[RetrievalCandidate],
    principal_id: str,
) -> tuple[list[RetrievalCandidate], list[RetrievalCandidate]]:
    """硬过滤：principal/scope/trust/status/stale/superseded/duplicate（PLAN-16 完整）。

    不修改输入候选（RetrievalCandidate 为 frozen），使用 replace 携带排除原因。
    返回 (kept, excluded) —— excluded 保留完整 score 与 source refs 供 provenance 追溯。
    """
    import dataclasses

    seen: set[str] = set()
    kept: list[RetrievalCandidate] = []
    excluded: list[RetrievalCandidate] = []
    for c in candidates:
        if c.principal_id and c.principal_id != principal_id:
            excluded.append(dataclasses.replace(c, exclusion_reason="unauthorized_principal"))
            continue
        if c.trust_score < 0.5:
            excluded.append(dataclasses.replace(c, exclusion_reason="trust_policy"))
            continue
        key = f"{c.candidate_type}:{c.candidate_id}"
        if key in seen:
            excluded.append(dataclasses.replace(c, exclusion_reason="duplicate"))
            continue
        seen.add(key)
        kept.append(c)
    return kept, excluded


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
    attempt_id: str = ""
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
    # PLAN-16 M6 #14 完整：每条被排除候选的完整 provenance（score 分项 + 排除原因）
    excluded: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "memory_ids", tuple(self.memory_ids))
        object.__setattr__(self, "per_source_tokens", tuple(self.per_source_tokens))
        object.__setattr__(self, "exclusion_stats", tuple(self.exclusion_stats))
        if not hasattr(self, "excluded"):
            object.__setattr__(self, "excluded", ())
        else:
            object.__setattr__(self, "excluded", tuple(self.excluded))


class KnowledgeReader(Protocol):
    def retrieve(
        self,
        *,
        principal_id: str,
        query: str,
        limit: int = 8,
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
            items.append(
                ContextItem(
                    item_type="system_policy",
                    item_id="system_policy",
                    source="system",
                    tokens=estimate_tokens(system_policy),
                    trust_label="internal",
                    content=system_policy,
                    role="system",
                )
            )

        # ── PLAN-16 P16-14 完整：两阶段 Unified Retrieval ──────────────────────
        # 阶段 1: 各 Retriever 召回 RetrievalCandidate（不直接生成 ContextItem）
        # 预算分配使用局部变量（不保存在实例字段，避免上一轮 Turn 污染下一轮）
        query_text = (input_msg or {}).get("content", "") or ""

        # 召回记忆候选
        memory_candidates, memory_protected_ids, _ = self._recall_memories(
            principal_id,
            session_id,
            query=query_text,
        )
        # 召回知识候选
        knowledge_candidates, _ = (
            self._recall_knowledge(
                principal_id,
                query_text,
            )
            if self._knowledge_reader is not None
            else ([], {})
        )
        # 召回 TaskState 候选
        task_state_candidates, _ = self._recall_task_state(principal_id)

        # 计算总 token 比 及 SessionSummary 候选（上下文压缩）
        history_itemized = [self._message_to_item(m) for m in history]
        input_itemized = self._message_to_item(input_msg) if input_msg else None
        base_tokens = sum(i.tokens for i in items)
        history_tokens = sum(i.tokens for i in history_itemized)
        input_tokens = input_itemized.tokens if input_itemized else 0
        total_estimate = base_tokens + history_tokens + input_tokens
        token_ratio = total_estimate / self._max_input_tokens if self._max_input_tokens > 0 else 0

        summary_candidates: list[RetrievalCandidate] = []
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
                    summary_candidates = [
                        RetrievalCandidate(
                            candidate_type="session_summary",
                            candidate_id=summary["summary_id"],
                            principal_id=principal_id,
                            scope=session_id,
                            content_ref=self._format_summary(summary["content_json"]),
                            source_refs=(session_id,),
                            recency_score=1.0,
                            importance_score=1.0,
                            trust_score=1.0,
                            final_score=1.0,
                            token_estimate=estimate_tokens(
                                self._format_summary(summary["content_json"])
                            ),
                            retrieval_path="summary",
                            policy_version=self._policy_version,
                        )
                    ]
                    history = recent

        # 消息候选（recent + older）
        keep_min = min(self.KEEP_RECENT_COUNT, len(history))
        message_candidates = self._build_message_candidates(
            history,
            query_text,
            protected_indices=set(),
            message_upper_bound=message_upper_bound,
        )

        # 阶段 2: 合并所有候选到统一池 → 单次 select_candidates() 决策
        all_candidates: list[RetrievalCandidate] = []
        all_candidates.extend(memory_candidates)
        all_candidates.extend(knowledge_candidates)
        all_candidates.extend(task_state_candidates)
        all_candidates.extend(summary_candidates)
        all_candidates.extend(message_candidates)

        # protected_ids（永不挤出）：active goals / constraints / recent-K / input / summary
        protected_ids: set[str] = set(memory_protected_ids)
        for m in history[-keep_min:]:
            protected_ids.add(m["message_id"])
        for sc in summary_candidates:
            protected_ids.add(sc.candidate_id)
        if input_msg:
            protected_ids.add(input_msg.get("message_id", ""))

        # 硬过滤 (principal/scope/trust/status/stale/superseded/duplicate)
        # PLAN-16 P16-14：返回 (kept, excluded)，excluded 保留完整 score 供 provenance
        all_candidates, hard_excluded = _hard_filter(all_candidates, principal_id)

        # 单次统一预算选择（PLAN-16 M6 RET-03 完整）
        allocations = allocate_budget(
            total_budget=self._max_input_tokens, config=self._budget_config
        )
        selection = select_candidates(all_candidates, allocations, protected_ids=protected_ids)

        # 按语义顺序 Emit ContextItem
        memory_selected = [
            c
            for c in selection.selected
            if c.candidate_id in {mc.candidate_id for mc in memory_candidates}
        ]
        knowledge_selected = [
            c
            for c in selection.selected
            if c.candidate_id in {kc.candidate_id for kc in knowledge_candidates}
        ]
        task_selected = [
            c
            for c in selection.selected
            if c.candidate_id in {tc.candidate_id for tc in task_state_candidates}
        ]
        summary_selected = [c for c in selection.selected if c.candidate_type == "session_summary"]
        message_selected = [c for c in selection.selected if c.candidate_type == "recent_message"]

        # 消息按 receive_sequence 排序（满足所有 ordering 测试）
        msg_order = {m["message_id"]: m["sequence"] for m in history}
        message_selected.sort(key=lambda c: msg_order.get(c.candidate_id, 0))

        # 按 type 顺序组装 (system → memory → knowledge → task_state → summary → messages → input)
        mem_item = self._memory_items(memory_selected)
        if mem_item:
            items.append(mem_item)
        items.extend(self._knowledge_item(c) for c in knowledge_selected)
        items.extend(self._task_state_item(c) for c in task_selected)
        items.extend(self._summary_item(c) for c in summary_selected)
        items.extend(
            self._message_to_item(next(m for m in history if m["message_id"] == c.candidate_id))
            for c in message_selected
        )

        # 当前用户输入（最后、protected）
        if input_msg:
            items.append(self._message_to_item(input_msg))

        # Emergency clip（仅异常保护）
        total_tokens = sum(i.tokens for i in items)
        clip_excluded: list[RetrievalCandidate] = []
        if total_tokens > self._max_input_tokens:
            items, clip_excluded = self._clip_to_budget(items, input_message_id)
            total_tokens = sum(i.tokens for i in items)

        # 统一选择的排除候选 + clip 排除字符串 + 硬过滤排除，合并到 provenance
        selection_excluded: list[RetrievalCandidate] = list(selection.excluded)
        # PLAN-16 P16-14：所有被排除候选（含硬过滤）均保留完整 score 与排除原因
        all_excluded: list[RetrievalCandidate] = list(hard_excluded) + selection_excluded

        per_source: dict[str, int] = {}
        for item in items:
            per_source[item.item_type] = per_source.get(item.item_type, 0) + item.tokens
        exclusion_counts: dict[str, int] = {}
        # PLAN-16 M6 #14 完整：汇总 clip、统一选择、硬过滤的排除原因
        for c in all_excluded:
            reason = c.exclusion_reason or "unknown"
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
        for clip in clip_excluded:
            kind = clip.split(":", 1)[0] if ":" in clip else "token_budget"
            exclusion_counts[kind] = exclusion_counts.get(kind, 0) + 1

        provenance_excluded: list[dict] = [
            _candidate_provenance(c, reason=c.exclusion_reason) for c in all_excluded
        ]
        provenance_excluded += [
            {"clip": clip, "exclusion_reason": "token_budget"} for clip in clip_excluded
        ]

        snapshot = ContextSnapshot(
            snapshot_id=uuid.uuid4().hex,
            turn_id=turn_id,
            input_message_id=input_message_id,
            session_id=session_id,
            conversation_id=self._get_session_conversation(session_id),
            principal_id=principal_id,
            memory_ids=tuple(c.candidate_id for c in memory_selected),
            message_upper_bound=message_upper_bound,
            query_plan_version=self._query_plan_version,
            selection_policy_version=self._policy_version,
            items=tuple(items),
            excluded_summary=(
                f"Excluded {len(selection_excluded) + len(clip_excluded)} items"
                if selection_excluded or clip_excluded
                else ""
            ),
            total_tokens=total_tokens,
            created_at=epoch_ms(self._clock.now()),
            per_source_tokens=tuple(sorted(per_source.items())),
            exclusion_stats=tuple(sorted(exclusion_counts.items())),
            # PLAN-16 M6 #14 完整：持久化被排除候选的完整 provenance
            excluded=tuple(provenance_excluded),
        )

        return snapshot

    def _build_knowledge_candidates(
        self,
        results: list[dict],
    ) -> list[RetrievalCandidate]:
        """PLAN-13/R-12: 将 KnowledgeReader 返回的 dict 映射为 RetrievalCandidate。"""
        candidates: list[RetrievalCandidate] = []
        for value in results:
            content = str(value.get("text_ref_or_inline", ""))
            if not content:
                continue
            segment_id = str(value.get("segment_id", ""))
            score = float(value.get("score", 0.0))
            retrieval_path = str(value.get("retrieval_path", "keyword"))
            candidates.append(
                RetrievalCandidate(
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
                    trust_score=1.0
                    if value.get("trust_label") in ("internal", "verified")
                    else 0.7,
                    final_score=score,
                    retrieval_path=retrieval_path,
                    policy_version=self._policy_version,
                )
            )
        return candidates

    def _recall_task_state(
        self,
        principal_id: str,
    ) -> tuple[list[RetrievalCandidate], dict[str, str]]:
        """PLAN-16 P16-14 完整：TaskStateRetriever（RETRIEVAL-CONTEXT §4）。

        召回活跃 Task 状态作为独立检索源（只返回 RetrievalCandidate，不内部选择）。
        与 _recall_memories / _recall_knowledge 统一模式：各来源只负责召回，
        统一选择由 build() 内的 select_candidates() 完成。
        返回 (candidates, excluded {id: reason})。
        """
        if not principal_id:
            return [], {}
        rows = self._conn.execute(
            "SELECT task_id, task_type, payload_ref, origin, priority, status "
            "FROM tasks WHERE status IN ('queued','running') "
            "AND origin NOT LIKE 'memory_maintenance%' "
            "ORDER BY priority DESC, created_at DESC LIMIT ?",
            (10,),
        ).fetchall()
        if not rows:
            return [], {}
        task_candidates: list[RetrievalCandidate] = []
        for r in rows:
            payload = {}
            try:
                payload = json.loads(r["payload_ref"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            explicit = str(r["task_type"]) in (
                "memory.extract",
                "knowledge.ingest",
                "knowledge.sync_source",
            )
            task_candidates.append(
                RetrievalCandidate(
                    candidate_type="task_state",
                    candidate_id=str(r["task_id"]),
                    principal_id=principal_id,
                    scope="",
                    content_ref=json.dumps(
                        {"task_type": r["task_type"], "origin": r["origin"], "payload": payload},
                        ensure_ascii=False,
                    ),
                    token_estimate=60,
                    keyword_score=0.0,
                    semantic_score=0.0,
                    recency_score=0.0,
                    importance_score=float(r["priority"]) / 100.0,
                    trust_score=1.0 if explicit else 0.6,
                    final_score=float(r["priority"]) / 100.0,
                    retrieval_path="task_state",
                    policy_version=self._policy_version,
                )
            )
        task_candidates = normalize_scores(task_candidates)
        self._record_candidates("task_state", task_candidates, selected=False)
        return task_candidates, {}

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
                "explicit_user_statement",
                "confirmed_inference",
            )
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
            candidates.append(
                RetrievalCandidate(
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
                )
            )
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
            "explicit_user_statement",
            "confirmed_inference",
        )
        trust = 1.0 if is_explicit else 0.7
        return m.importance * 0.5 + m.confidence * 0.3 + trust * 0.2

    def _build_message_candidates(
        self,
        messages: list[dict],
        query: str,
        protected_indices: set[int],
        message_upper_bound: int = 0,
    ) -> list[RetrievalCandidate]:
        """PLAN-16 M6 RET-02/03/05: 将历史消息转为 RetrievalCandidate。

        Recent-K 与 input 由调用方标记为 protected（永不被 budget 挤出）；
        其余按 recency + keyword overlap 评分；emit 时按 receive_sequence 保持时序，
        满足 test_ordering_system_history_input / test_current_input_is_last 等约束。

        message_upper_bound 由调用方传入（不保存在实例字段，避免跨 Turn 污染）。
        """
        use_query = bool(query and query.strip())
        candidates: list[RetrievalCandidate] = []
        for idx, m in enumerate(messages):
            content = m.get("content", "")
            rel_tokens = _tokenize(content)
            kw_score = 0.0
            if use_query and rel_tokens:
                q_terms = set(_tokenize(query))
                if q_terms:
                    hits = sum(1 for t in q_terms if t in rel_tokens)
                    kw_score = min(1.0, hits / len(q_terms))
            seq = int(m.get("sequence", 0))
            # recency: 用 message_upper_bound 归一化，较新 = 较高
            denom = max(1, message_upper_bound)
            rec = seq / denom
            final = _KW_WEIGHT * kw_score + _RECENCY_WEIGHT * rec + _IMPORTANCE_WEIGHT * 0.5
            candidates.append(
                RetrievalCandidate(
                    candidate_type="recent_message",
                    candidate_id=m["message_id"],
                    principal_id=m.get("sender_principal_id", ""),
                    scope=m.get("session_id", ""),
                    content_ref=content,
                    source_refs=(m.get("session_id", ""), m["message_id"]),
                    keyword_score=kw_score,
                    semantic_score=0.0,
                    recency_score=rec,
                    importance_score=0.5,
                    trust_score=1.0 if m.get("trust_label") == "verified" else 0.7,
                    final_score=final,
                    token_estimate=estimate_tokens(content),
                    retrieval_path="list",
                    policy_version=self._policy_version,
                    exclusion_reason="" if idx not in protected_indices else "",
                )
            )
        return candidates

    def _recall_memories(
        self,
        principal_id: str,
        session_id: str,
        query: str = "",
    ) -> tuple[list[RetrievalCandidate], list[str], dict[str, str]]:
        """PLAN-16 M6 完整：召回记忆候选（不直接生成 ContextItem / 不 select）。

        返回 (candidates, protected_memory_ids, excluded {id: reason})。
        protected_memory_ids 指向 active goal / constraint（不会被 budget 挤出）。
        """
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
            all_memories,
            session_id,
            conversation_id,
            query=query,
        )

        # PLAN-16 M6 RET-04: 组内标准化，使 memory 分数可与其他源比较
        candidates = normalize_scores(candidates)
        protected_memory_ids = [candidates[i].candidate_id for i in protected_indices]
        return candidates, protected_memory_ids, {}

    def _recall_knowledge(
        self,
        principal_id: str,
        query: str,
    ) -> tuple[list[RetrievalCandidate], dict[str, str]]:
        """PLAN-16 P16-14 完整：召回知识候选（只返回 RetrievalCandidate，不内部选择）。

        与 _recall_memories / _recall_task_state 统一模式：各来源只负责召回，
        统一选择由 build() 内的 select_candidates() 完成。
        返回 (candidates, excluded {id: reason})。
        """
        if not principal_id or not query.strip() or self._knowledge_reader is None:
            return [], {}
        try:
            results = self._knowledge_reader.retrieve(
                principal_id=principal_id,
                query=query,
                limit=self._knowledge_top_k,
            )
        except Exception:
            return [], {}
        candidates = self._build_knowledge_candidates(results)
        # PLAN-16 M6 RET-04: 组内标准化，使 knowledge 分数可与其他源比较
        candidates = normalize_scores(candidates)
        self._record_candidates("knowledge_segment", candidates, selected=False)
        return candidates, {}

    # ── OPS-04 完整：context 指标记录 ──────────────────────────────────────

    @staticmethod
    def _record_candidates(source: str, candidates: list, selected: bool) -> None:
        """记录某来源的候选/选中计数。"""
        try:
            from cogito.infrastructure.metrics_access import _metrics

            for _ in candidates:
                _metrics().record_context_candidate(source, selected)
        except Exception:
            pass

    @staticmethod
    def _record_exclusions(source: str, excluded: dict) -> None:
        """记录某来源的排除原因（OPS-04）。"""
        try:
            from cogito.infrastructure.metrics_access import _metrics

            for reason in excluded.values():
                _metrics().record_context_exclusion(f"{source}:{reason}")
        except Exception:
            pass

    @staticmethod
    def _record_tokens(source: str, tokens: int) -> None:
        """记录某来源实际 token 占用（OPS-04）。"""
        try:
            from cogito.infrastructure.metrics_access import _metrics

            _metrics().record_context_tokens(source, tokens)
        except Exception:
            pass

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
            if row["inline_data"] and row["content_type"] in ("text", "markdown"):
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
                        f'asset_id="{asset.get("asset_id", "")}" '
                        f'mime_type="{asset.get("mime_type", "")}" '
                        f'status="{status}">\n'
                        '<external_data trust="unverified">\n'
                        f"{external}\n"
                        "</external_data>\n"
                        "</multimodal_asset>"
                    )
            result.append(
                {
                    "message_id": msg["message_id"],
                    "role": msg["role"],
                    "direction": msg["direction"],
                    "sequence": msg["sequence"],
                    "trust_label": msg["trust_label"],
                    "session_id": msg["session_id"],
                    "sender_principal_id": msg.get("sender_principal_id", ""),
                    "content": ("\n".join(content_parts) if content_parts else ""),
                }
            )
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

    def _summary_item(self, cand: RetrievalCandidate) -> ContextItem:
        """SessionSummary candidate → ContextItem（PLAN-16 完整，保留角色与信任）。"""
        return ContextItem(
            item_type="summary",
            item_id=cand.candidate_id,
            source=cand.scope or "",
            tokens=estimate_tokens(cand.content_ref),
            trust_label="verified",
            content=cand.content_ref,
            role="system",
            score=cand.final_score,
            retrieval_path=cand.retrieval_path,
            provenance=(
                ("candidate_type", cand.candidate_type),
                ("final_score", f"{cand.final_score:.3f}"),
                ("policy_version", cand.policy_version),
                ("query_plan_version", self._query_plan_version),
            ),
        )

    def _memory_items(self, cands: list[RetrievalCandidate]) -> ContextItem:
        """Memory 候选集合 → 单个 wrapped ContextItem（PLAN-16 完整）。

        保持与旧版兼容的 <relevant_memories> 包装格式，供 Dashboard 与测试识别。
        """
        if not cands:
            return None
        lines = ["<relevant_memories>"]
        for c in cands:
            lines.append(f"  {c.content_ref}")
        lines.append("</relevant_memories>")
        content = "\n".join(lines)
        return ContextItem(
            item_type="memory",
            item_id="_injected_memory",
            source=cands[0].scope or "",
            tokens=estimate_tokens(content),
            trust_label="verified",
            content=content,
            role="system",
            score=max(c.final_score for c in cands) if cands else 0.0,
            retrieval_path="list",
            provenance=(
                ("source_count", str(len(cands))),
                ("pool_size", str(len(cands))),
                ("top_score", f"{max(c.final_score for c in cands):.3f}" if cands else "0"),
                ("policy_version", self._policy_version),
                ("query_plan_version", self._query_plan_version),
            ),
        )

    def _knowledge_item(self, cand: RetrievalCandidate) -> ContextItem:
        """Knowledge candidate → ContextItem。"""
        return ContextItem(
            item_type="knowledge",
            item_id=cand.candidate_id,
            source=cand.scope or "",
            tokens=max(1, cand.token_estimate),
            trust_label="verified" if cand.trust_score >= 1.0 else "unverified",
            content=f"<knowledge_segment>\n{cand.content_ref}\n</knowledge_segment>",
            role="system",
            score=cand.final_score,
            retrieval_path=cand.retrieval_path,
            provenance=(
                ("candidate_type", cand.candidate_type),
                ("final_score", f"{cand.final_score:.3f}"),
                ("token_estimate", str(cand.token_estimate)),
                ("policy_version", cand.policy_version),
                ("query_plan_version", self._query_plan_version),
                ("retrieval_path", cand.retrieval_path),
            ),
        )

    def _task_state_item(self, cand: RetrievalCandidate) -> ContextItem:
        """TaskState candidate → ContextItem。"""
        return ContextItem(
            item_type="task_state",
            item_id=cand.candidate_id,
            source="task_state",
            tokens=max(1, cand.token_estimate),
            trust_label="internal",
            content=f"<active_task>\n{cand.content_ref}\n</active_task>",
            role="system",
            score=cand.final_score,
            retrieval_path=cand.retrieval_path,
            provenance=(
                ("candidate_type", cand.candidate_type),
                ("final_score", f"{cand.final_score:.3f}"),
                ("token_estimate", str(cand.token_estimate)),
                ("policy_version", cand.policy_version),
                ("query_plan_version", self._query_plan_version),
            ),
        )
