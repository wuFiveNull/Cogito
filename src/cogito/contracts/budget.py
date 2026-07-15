"""Token Budget 分配（PLAN-13 P13-12 M6）。

per-source 预算配额，版本化策略。knowledge 不能挤掉 active constraint
和当前 Task state。未使用额度可转移。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cogito.contracts.retrieval import RetrievalCandidate


@dataclass
class TokenBudgetConfig:
    """Token Budget 配额（PLAN-13 §13.4，可评测配置）。"""
    version: str = "1"
    # 各源占总预算占比（除 System Policy 和当前输入外）
    recent_messages_ratio: float = 0.30
    session_summary_ratio: float = 0.10
    fact_memory_ratio: float = 0.20
    goals_constraints_ratio: float = 0.10
    knowledge_segments_ratio: float = 0.20
    task_tool_state_ratio: float = 0.10
    # 最低保障 token（knowledge 不能挤掉这些）
    min_tokens_recent_messages: int = 1000
    min_tokens_goals_constraints: int = 500
    min_tokens_task_state: int = 300

    def quota(self, source: str, total_budget: int) -> int:
        """计算某源的 Token 配额。"""
        ratios = {
            "recent_message": self.recent_messages_ratio,
            "session_summary": self.session_summary_ratio,
            "memory": self.fact_memory_ratio,
            "goal": self.goals_constraints_ratio,
            "knowledge_segment": self.knowledge_segments_ratio,
            "task_state": self.task_tool_state_ratio,
        }
        return max(0, int(total_budget * ratios.get(source, 0.0)))

    def min_tokens(self, source: str) -> int:
        """最低保障 token（PLAN-13 §13.4 不可挤掉）。"""
        mins = {
            "recent_message": self.min_tokens_recent_messages,
            "goal": self.min_tokens_goals_constraints,
            "task_state": self.min_tokens_task_state,
        }
        return mins.get(source, 0)


@dataclass
class BudgetAllocation:
    """实际分配结果。"""
    source: str
    quota: int
    used: int = 0
    selected: list[str] = field(default_factory=list)  # 选中的 item/segment IDs
    excluded_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.quota - self.used)


@dataclass
class BudgetSelection:
    """统一预算选择结果（PLAN-16 RET-03 完整）。"""

    selected: list[RetrievalCandidate] = field(default_factory=list)
    excluded: list[RetrievalCandidate] = field(default_factory=list)
    allocations: dict[str, BudgetAllocation] = field(default_factory=dict)


def allocate_budget(
    *,
    total_budget: int,
    config: TokenBudgetConfig | None = None,
) -> dict[str, BudgetAllocation]:
    """初始化各源预算分配。"""
    config = config or TokenBudgetConfig()
    sources = [
        "recent_message", "session_summary", "memory",
        "goal", "knowledge_segment", "task_state",
    ]
    return {
        s: BudgetAllocation(source=s, quota=config.quota(s, total_budget))
        for s in sources
    }


def select_candidates(
    candidates: list[RetrievalCandidate],
    allocations: dict[str, BudgetAllocation],
    *,
    protected_ids: set[str] | None = None,
) -> BudgetSelection:
    """统一候选选择（PLAN-16 RET-03 完整）。

    所有来源候选进入同一池；protected_ids 指向 active goal / constraint /
    recent message / input，永不挤出；各源按配额选择，未用余额进入共享池；
    无法容纳的候选标记排除原因。
    """
    from .context import normalize_scores

    protected_ids = protected_ids or set()
    # 按 source group 内部归一化（各来源 score 可比）
    by_source: dict[str, list[RetrievalCandidate]] = {}
    for c in candidates:
        by_source.setdefault(c.candidate_type, []).append(c)
    for group in by_source.values():
        normalize_scores(group)

    # protected 先选（不计入 quota 限制）
    protected_selected: list[RetrievalCandidate] = [
        c for c in candidates if c.candidate_id in protected_ids
    ]

    # 普通候选按 final_score 排序 + 按 source quota 选择
    pool = sorted(
        [c for c in candidates if c.candidate_id not in protected_ids],
        key=lambda c: -c.final_score,
    )
    import dataclasses
    selected: list[RetrievalCandidate] = list(protected_selected)
    excluded: list[RetrievalCandidate] = []
    for alloc in allocations.values():
        alloc.used = 0
        alloc.selected = []
        alloc.excluded_reasons = {}

    for c in pool:
        alloc = allocations.get(c.candidate_type)
        if alloc is None:
            # PLAN-16 P16-14：frozen 候选用 replace 携带排除原因
            excluded.append(dataclasses.replace(c, exclusion_reason="no_allocation"))
            continue
        if alloc.used + max(1, c.token_estimate) <= alloc.quota:
            selected.append(c)
            alloc.used += max(1, c.token_estimate)
            alloc.selected.append(c.candidate_id)
        else:
            # 本来源配额用完，尝试共享池
            total_remaining = sum(a.remaining for a in allocations.values())
            if total_remaining >= max(1, c.token_estimate):
                selected.append(c)
                fullest = max(allocations.values(), key=lambda a: a.remaining)
                fullest.used += max(1, c.token_estimate)
                fullest.selected.append(c.candidate_id)
            else:
                excluded.append(dataclasses.replace(c, exclusion_reason="token_budget"))

    return BudgetSelection(selected=selected, excluded=excluded, allocations=allocations)
