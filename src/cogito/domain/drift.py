"""Drift 领域模型 —— Drift 是 Task mode，不是新领域根 (DOMAIN-CONTRACTS / 1.10)。

Drift 复用 tasks / task_attempts 作为生命周期权威；本模块仅保存
Drift 专属属性（Skill 选择、准入快照、检查点、结果）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class DriftReasonCode(StrEnum):
    """Admission / 抢占 / 结束的 deterministic reason code。"""
    # admission deny reasons
    active_turn = "active_turn"
    priority_backlog = "priority_backlog"
    delivery_backlog = "delivery_backlog"
    outbox_critical = "outbox_critical"
    recovery_in_progress = "recovery_in_progress"
    resource_pressure = "resource_pressure"
    budget_exhausted = "budget_exhausted"
    not_idle_long_enough = "not_idle_long_enough"
    drift_already_active = "drift_already_active"
    # finish reasons
    completed = "completed"
    failed = "failed"
    skipped_no_value = "skipped_no_value"
    paused_budget_exhausted = "paused_budget_exhausted"
    preempted_by_turn = "preempted_by_turn"
    lease_lost = "lease_lost"


class DriftRunStatus(StrEnum):
    admitted = "admitted"
    running = "running"
    waiting = "waiting"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    needs_review = "needs_review"


@dataclass(frozen=True)
class DriftSkillManifest:
    """Skill 的声明式 manifest（机器约束，不是授权）。

    manifest 声明的工具仍需 Capability Policy 逐次授权；声明本身不能放行权限。
    """
    name: str
    version: str = "1.0"
    description: str = ""
    handler: str = ""                       # 内置 handler 路径
    risk_level: str = "low"                  # low | medium | high
    allowed_tools: tuple[str, ...] = ()
    max_steps: int = 6
    max_runtime_seconds: int = 30
    max_model_calls: int = 1
    max_tool_calls: int = 8
    can_emit_candidate: bool = False
    requires_approval: bool = False
    checkpoint_schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version,
            "description": self.description, "handler": self.handler,
            "risk_level": self.risk_level, "allowed_tools": list(self.allowed_tools),
            "max_steps": self.max_steps, "max_runtime_seconds": self.max_runtime_seconds,
            "max_model_calls": self.max_model_calls, "max_tool_calls": self.max_tool_calls,
            "can_emit_candidate": self.can_emit_candidate,
            "requires_approval": self.requires_approval,
            "checkpoint_schema_version": self.checkpoint_schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriftSkillManifest:
        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "1.0")),
            description=str(data.get("description", "")),
            handler=str(data.get("handler", "")),
            risk_level=str(data.get("risk_level", "low")),
            allowed_tools=tuple(str(v) for v in data.get("allowed_tools", ())),
            max_steps=int(data.get("max_steps", 6)),
            max_runtime_seconds=int(data.get("max_runtime_seconds", 30)),
            max_model_calls=int(data.get("max_model_calls", 1)),
            max_tool_calls=int(data.get("max_tool_calls", 8)),
            can_emit_candidate=bool(data.get("can_emit_candidate", False)),
            requires_approval=bool(data.get("requires_approval", False)),
            checkpoint_schema_version=int(data.get("checkpoint_schema_version", 1)),
        )


@dataclass(frozen=True)
class DriftAdmissionSnapshot:
    """单次 admission 读取的全局 idle 快照（确定性、事务性读取）。"""
    active_normal_turns: int = 0
    high_priority_task_backlog: int = 0
    ready_delivery_backlog: int = 0
    outbox_critical_age_ms: int = 0
    recovery_in_progress: bool = False
    last_user_activity_age_ms: int | None = None
    daily_drift_budget_remaining: int = 0
    drift_already_active: bool = False
    snapshot_at: int = 0  # epoch ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_normal_turns": self.active_normal_turns,
            "high_priority_task_backlog": self.high_priority_task_backlog,
            "ready_delivery_backlog": self.ready_delivery_backlog,
            "outbox_critical_age_ms": self.outbox_critical_age_ms,
            "recovery_in_progress": self.recovery_in_progress,
            "last_user_activity_age_ms": self.last_user_activity_age_ms,
            "daily_drift_budget_remaining": self.daily_drift_budget_remaining,
            "drift_already_active": self.drift_already_active,
            "snapshot_at": self.snapshot_at,
        }


@dataclass
class DriftCheckpointV1:
    """Drift 单步检查点 (schema_version=1)。

    不得保存 Secret、原始大 Payload、未脱敏 Tool 输出或可变 Provider 对象。
    """
    drift_run_id: str
    task_id: str
    attempt_id: str
    skill_name: str
    skill_version: str = "1.0"
    step_index: int = 0
    cursor: dict[str, Any] = field(default_factory=dict)
    completed_actions: list[str] = field(default_factory=list)
    pending_action: str | None = None
    budget_used: dict[str, int] = field(default_factory=dict)
    config_version_id: str = ""
    capability_snapshot_version: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "drift_run_id": self.drift_run_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "step_index": self.step_index,
            "cursor": self.cursor,
            "completed_actions": list(self.completed_actions),
            "pending_action": self.pending_action,
            "budget_used": dict(self.budget_used),
            "config_version_id": self.config_version_id,
            "capability_snapshot_version": self.capability_snapshot_version,
        }


@dataclass(frozen=True)
class DriftCandidateDraft:
    """Drift 完成后投影为用户可见 Candidate 的草稿（R9 M6）。

    投影服务校验来源/PO，生成幂等 ProactiveCandidate(origin=drift)。
    """
    topic: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    trust_label: str = "system_generated"   # unverified | system_generated | confirmed_inference
    urgency: float = 0.5
    confidence: float = 0.5
    relevance: float = 0.5
    expires_at: int | None = None           # epoch ms
    suggested_target: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic, "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "trust_label": self.trust_label, "urgency": self.urgency,
            "confidence": self.confidence, "relevance": self.relevance,
            "expires_at": self.expires_at,
            "suggested_target": self.suggested_target,
        }


@dataclass
class DriftRunResult:
    """Drift run 完成后的内部结果（不含用户可见 Candidate）。"""
    drift_run_id: str
    status: DriftRunStatus = DriftRunStatus.completed
    summary: str = ""
    internal_items: list[dict[str, Any]] = field(default_factory=list)
    candidate_emitted: bool = False
    candidate_id: str | None = None
    started_at: int = 0
    finished_at: int = 0
