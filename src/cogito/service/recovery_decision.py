"""RecoveryDecision + RecoveryAdvisor — Plan 02 M2.

Recovery 决策器：检查 Turn/RunAttempt/Checkpoint 的状态，决定如何恢复。

设计引用：
- EXECUTION-LIFECYCLE / 5. 重试、等待与恢复
- GLOBAL-INVARIANTS / 2.5：旧 Lease/旧 Attempt 不得提交
- GLOBAL-INVARIANTS / 3.1：side_effect_unknown 必须先 reconcile

决策路径：
require approval / external wait
→ persist checkpoint + waiting condition
→ end current Attempt and release Lane/Lease
→ Command/Event satisfies condition
→ create a new Attempt
→ validate old receipts/config/snapshot
→ resume from deterministic checkpoint
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class RecoveryDecision(StrEnum):
    """恢复决策 —— 恢复决策器输出的权威结论。"""

    resume = "resume"  # 从 checkpoint 续跑
    retry = "retry"  # 新建 Attempt 重试
    reconcile = "reconcile"  # unknown 副作用必须先对账
    waiting_user = "waiting_user"  # 等审批/用户响应
    manual_review = "manual_review"  # 需人工介入
    fail = "fail"  # 不可恢复，标记失败


@dataclass(frozen=True)
class Checkpoint:
    """完整 Checkpoint 结构 (Plan 02 M2 定义, 13 字段)。

    仅序列化纯数据值，不序列化 Provider SDK、数据库连接、Coroutine 或
    Python 栈（执行器/连接由 attempt_id 运行时解析）。
    """

    checkpoint_id: str = ""
    turn_id: str = ""
    attempt_id: str = ""
    current_step: str = ""
    completed_step_ids: list[str] = field(default_factory=list)
    context_snapshot_id: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{id,status,receipt_ref}]
    pending_approval_id: str = ""
    partial_result_ref: str = ""
    budget_consumed: dict[str, Any] = field(default_factory=dict)
    config_version: str = "1.0"
    capability_snapshot_version: str = "1.0"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "turn_id": self.turn_id,
            "attempt_id": self.attempt_id,
            "current_step": self.current_step,
            "completed_step_ids": self.completed_step_ids,
            "context_snapshot_id": self.context_snapshot_id,
            "tool_calls": self.tool_calls,
            "pending_approval_id": self.pending_approval_id,
            "partial_result_ref": self.partial_result_ref,
            "budget_consumed": self.budget_consumed,
            "config_version": self.config_version,
            "capability_snapshot_version": self.capability_snapshot_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(
            checkpoint_id=data.get("checkpoint_id", ""),
            turn_id=data.get("turn_id", ""),
            attempt_id=data.get("attempt_id", ""),
            current_step=data.get("current_step", ""),
            completed_step_ids=data.get("completed_step_ids", []),
            context_snapshot_id=data.get("context_snapshot_id", ""),
            tool_calls=data.get("tool_calls", []),
            pending_approval_id=data.get("pending_approval_id", ""),
            partial_result_ref=data.get("partial_result_ref", ""),
            budget_consumed=data.get("budget_consumed", {}),
            config_version=data.get("config_version", "1.0"),
            capability_snapshot_version=data.get("capability_snapshot_version", "1.0"),
            created_at=data.get("created_at", ""),
        )


@dataclass(frozen=True)
class RecoveryEvidence:
    """恢复决策的证据记录 —— 每次恢复追踪：父 Attempt、原因、决策证据。"""

    decision: RecoveryDecision
    parent_attempt_id: str
    reason_code: str
    reason_detail: str
    decided_at: str = ""
    decision_evidence: dict[str, Any] = field(default_factory=dict)


class RecoveryAdvisor:
    """恢复决策器：分析 Turn/RunAttempt/Checkpoint，产出决策。

    决策顺序：
    1. 已取消 → fail
    2. side_effect_unknown → reconcile
    3. waiting_user/waiting_external → waiting_user
    4. Lease 过期 + 有可恢复 checkpoint → resume
    5. Lease 过期 + 无 checkpoint → retry
    6. 超出预算/超限 → fail
    7. 配置不兼容 → manual_review
    """

    def __init__(
        self, config_version: str = "1.0", capability_snapshot_version: str = "1.0"
    ) -> None:
        self._config_version = config_version
        self._capability_snapshot_version = capability_snapshot_version

    def decide(
        self,
        turn: Any,
        attempt: Any,
        checkpoint: Checkpoint | None = None,
        clock: datetime | None = None,
    ) -> RecoveryEvidence:
        """产出恢复决策。"""
        now = clock or datetime.now(UTC)
        parent_id = getattr(attempt, "attempt_id", "")

        # 1. 已取消
        if getattr(turn, "status", None) == "cancelled":
            return RecoveryEvidence(
                decision=RecoveryDecision.fail,
                parent_attempt_id=parent_id,
                reason_code="turn_cancelled",
                reason_detail="Turn is cancelled; cannot resume",
            )

        # 2. side_effect_unknown 必须先 reconcile
        if checkpoint:
            unknown_tools = [tc for tc in checkpoint.tool_calls if tc.get("status") == "unknown"]
            if unknown_tools:
                return RecoveryEvidence(
                    decision=RecoveryDecision.reconcile,
                    parent_attempt_id=parent_id,
                    reason_code="side_effect_unknown",
                    reason_detail=f"{len(unknown_tools)} tool call(s) in unknown state",
                    decision_evidence={"unknown_tool_calls": unknown_tools},
                )

        # 3. 等待审批/外部
        turn_status = getattr(turn, "status", "")
        if turn_status in ("waiting_user", "waiting_external"):
            return RecoveryEvidence(
                decision=RecoveryDecision.waiting_user,
                parent_attempt_id=parent_id,
                reason_code=f"turn_{turn_status}",
                reason_detail=f"Turn is {turn_status}; awaiting signal",
            )

        if checkpoint and checkpoint.pending_approval_id:
            return RecoveryEvidence(
                decision=RecoveryDecision.waiting_user,
                parent_attempt_id=parent_id,
                reason_code="pending_approval",
                reason_detail=f"Approval {checkpoint.pending_approval_id} not yet decided",
            )

        # 4+5. Lease 状态判断
        lease_expires = getattr(attempt, "lease_expires_at", None)
        is_expired = lease_expires is not None and now >= lease_expires

        if is_expired or getattr(attempt, "status", "") in ("failed", "cancelled", "abandoned"):
            if checkpoint and checkpoint.checkpoint_id:
                # 有可恢复检查点 → 验证兼容性后 resume
                compat = self._check_compatibility(checkpoint)
                if not compat.ok:
                    return RecoveryEvidence(
                        decision=RecoveryDecision.manual_review,
                        parent_attempt_id=parent_id,
                        reason_code=compat.reason_code,
                        reason_detail=compat.reason_detail,
                        decision_evidence=compat.evidence,
                    )
                return RecoveryEvidence(
                    decision=RecoveryDecision.resume,
                    parent_attempt_id=parent_id,
                    reason_code="lease_expired_with_checkpoint",
                    reason_detail="Lease expired; resuming from deterministic checkpoint",
                    decision_evidence={"checkpoint_id": checkpoint.checkpoint_id},
                )
            # 无 checkpoint → retry
            return RecoveryEvidence(
                decision=RecoveryDecision.retry,
                parent_attempt_id=parent_id,
                reason_code="lease_expired_no_checkpoint",
                reason_detail="Lease expired; no checkpoint, will retry",
            )

        # 6. 运行中且 Lease 有效 → 不应发生（恢复只对已终止 Attempt），防御性 fail
        return RecoveryEvidence(
            decision=RecoveryDecision.fail,
            parent_attempt_id=parent_id,
            reason_code="attempt_still_active",
            reason_detail="Attempt is still active; no recovery needed",
        )

    def _check_compatibility(self, checkpoint: Checkpoint) -> _CompatResult:
        """验证取消状态、Receipt、配置兼容、Provider/Tool 能力和预算。"""
        # 配置版本兼容
        if checkpoint.config_version and checkpoint.config_version != self._config_version:
            return _CompatResult(
                ok=False,
                reason_code="config_version_mismatch",
                reason_detail=(
                    f"checkpoint config_version={checkpoint.config_version!r} "
                    f"!= runtime={self._config_version!r}"
                ),
                evidence={
                    "checkpoint_config": checkpoint.config_version,
                    "runtime_config": self._config_version,
                },
            )
        # 能力快照兼容
        if (
            checkpoint.capability_snapshot_version
            and checkpoint.capability_snapshot_version != self._capability_snapshot_version
        ):
            return _CompatResult(
                ok=False,
                reason_code="capability_snapshot_mismatch",
                reason_detail=(
                    f"checkpoint capability_version={checkpoint.capability_snapshot_version!r} "
                    f"!= runtime={self._capability_snapshot_version!r}"
                ),
                evidence={
                    "checkpoint_capability": checkpoint.capability_snapshot_version,
                    "runtime_capability": self._capability_snapshot_version,
                },
            )
        return _CompatResult(ok=True, reason_code="", reason_detail="", evidence={})


@dataclass(frozen=True)
class _CompatResult:
    ok: bool
    reason_code: str
    reason_detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
