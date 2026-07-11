"""RetrievalCandidate — 统一检索候选协议（PLAN-13 P13-11 M6）。

所有检索源（recent message / session_summary / memory / goal /
knowledge_segment / task_state）统一映射为只读 RetrievalCandidate，
但保留各源硬过滤和独立召回实现。

禁止把 Store Entity 直接作为跨层返回类型。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalCandidate:
    """统一检索候选（PLAN-13 §5.4，不可变只读）。"""
    # recent_message | session_summary | memory | goal | knowledge_segment | task_state
    candidate_type: str = ""
    candidate_id: str = ""
    principal_id: str = ""
    scope: str = ""
    content_ref: str = ""  # 指向内容的引用（不直接暴露 Entity）
    source_refs: tuple[str, ...] = ()  # 来源引用列表
    keyword_score: float = 0.0
    semantic_score: float = 0.0
    recency_score: float = 0.0
    importance_score: float = 0.0
    trust_score: float = 0.0
    final_score: float = 0.0
    token_estimate: int = 0
    retrieval_path: str = ""  # 命中路径: keyword | semantic | keyword+semantic | list
    policy_version: str = "1"
    exclusion_reason: str = ""  # 非空表示被排除

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_refs", tuple(self.source_refs))


@dataclass(frozen=True)
class RetrievalResult:
    """检索结果（PLAN-13 §13）。"""
    candidates: tuple[RetrievalCandidate, ...] = ()
    excluded: tuple[RetrievalCandidate, ...] = ()
    total_hits: int = 0
    query_plan_version: str = "1"
    policy_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "excluded", tuple(self.excluded))
