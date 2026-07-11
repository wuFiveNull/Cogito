"""Token Budget 分配（PLAN-13 P13-12 M6）。

per-source 预算配额，版本化策略。knowledge 不能挤掉 active constraint
和当前 Task state。未使用额度可转移。
"""
from __future__ import annotations

from dataclasses import dataclass, field


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
