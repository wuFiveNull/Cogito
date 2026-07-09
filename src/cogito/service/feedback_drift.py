"""Feedback + Drift (Plan 04 M9).

- FeedbackEvent: opened/ignored/dismissed/useful/not_useful/muted/requested_more → Candidate
- Drift: Resource Manager 检查高优先级 backlog、保留并发、存储健康、恢复状态和日预算
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeedbackEvent:
    """用户反馈事件（写入 Outbox）。"""
    event_type: str  # opened|ignored|dismissed|useful|not_useful|muted|requested_more
    candidate_id: str = ""
    principal_id: str = ""
    channel: str = ""

    def to_preference_candidate(self) -> dict[str, Any]:
        """反馈生成 Preference Candidate，不直接永久调权（Plan 04 M9）。"""
        return {
            "source_type": "feedback",
            "source_event": self.event_type,
            "candidate_type": "preference",
            "principal_id": self.principal_id,
        }


class DriftController:
    """Drift 抢占控制器（Plan 04 M9）。"""

    ALLOWED_TASK_TYPES = {"memory_dedup", "embedding_rebuild", "index_rebuild",
                           "summary_refresh", "gc_scan", "view_check"}

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def should_preempt(self, *,
                       high_priority_backlog: int = 0,
                       active_normal_turns: int = 0,
                       daily_budget_remaining: float = 1.0) -> bool:
        """是否抢占当前 Drift 步骤。"""
        if active_normal_turns > 0:
            return True  # 新用户 Turn 到达时停止领取新步骤
        if high_priority_backlog > 5:
            return True
        if daily_budget_remaining < 0.1:
            return True
        return False

    def allowed_task_type(self, task_type: str) -> bool:
        """Drift 只创建正常 Task/Checkpoint，禁止发送/外部修改/确认 Memory/删除/安装。"""
        return task_type in self.ALLOWED_TASK_TYPES
