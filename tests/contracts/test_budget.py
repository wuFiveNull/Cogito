"""P13-12: Token Budget + Snapshot provenance tests."""

from __future__ import annotations

from cogito.contracts.budget import (
    BudgetAllocation,
    TokenBudgetConfig,
    allocate_budget,
)
from cogito.contracts.context import ContextItem, ContextSnapshot


class TestTokenBudget:
    def test_quota_sums_to_total(self):
        config = TokenBudgetConfig()
        total = 10000
        alloc = allocate_budget(total_budget=total, config=config)
        quotas_sum = sum(a.quota for a in alloc.values())
        # 各源配额之和 ≈ total（允许取整误差）
        assert abs(quotas_sum - total) < 100

    def test_min_tokens_guaranteed(self):
        """knowledge 不能挤掉 active constraint 和 task state。"""
        config = TokenBudgetConfig()
        assert config.min_tokens("goal") == 500
        assert config.min_tokens("task_state") == 300
        assert config.min_tokens("recent_message") == 1000

    def test_budget_remaining(self):
        alloc = BudgetAllocation(source="memory", quota=2000, used=500)
        assert alloc.remaining == 1500
        alloc2 = BudgetAllocation(source="memory", quota=100, used=200)
        assert alloc2.remaining == 0  # 不会为负


class TestContextProvenance:
    def test_context_item_provenance(self):
        """ContextItem 携带来源版本/score 分项（PLAN-13 §13.6）。"""
        item = ContextItem(
            item_type="memory",
            item_id="m1",
            source="session-1",
            score=0.8,
            retrieval_path="keyword",
            provenance=(
                ("source_version", "v2"),
                ("policy_version", "2"),
                ("trust", "high"),
            ),
        )
        assert item.provenance == (
            ("source_version", "v2"),
            ("policy_version", "2"),
            ("trust", "high"),
        )

    def test_snapshot_per_source_tokens(self):
        """Snapshot 记录各源实际 Token 分配。"""
        snap = ContextSnapshot(
            per_source_tokens=(
                ("memory", 2000),
                ("knowledge_segment", 1500),
                ("recent_message", 3000),
            ),
            exclusion_stats=(
                ("unauthorized", 2),
                ("low_score", 5),
            ),
        )
        assert len(snap.per_source_tokens) == 3
        assert snap.exclusion_stats == (("unauthorized", 2), ("low_score", 5))

    def test_backward_compatible(self):
        """不传新字段时行为不变。"""
        item = ContextItem(item_type="message", item_id="x", source="s1")
        assert item.provenance == ()
        snap = ContextSnapshot()
        assert snap.per_source_tokens == ()
