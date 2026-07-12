"""Pydantic 请求/响应模型 —— interaction-web 的 Query/Command API Contract。

ACCESS-DELIVERY §2.2 (Query API) / §2.3 (Command API)。
handler 与此模型交互；具体形状以 query_service / 命令执行为准。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ── 通用 ──────────────────────────────────────────────────────


class CommandResponse(BaseModel):
    """命令统一响应。"""
    command_id: str
    status: str
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


# ── Command 请求 ──────────────────────────────────────────────


class CancelTurnPayload(BaseModel):
    turn_id: str
    reason: str = "cancelled via dashboard"


class RetryTaskPayload(BaseModel):
    task_id: str


class ApprovalPayload(BaseModel):
    approval_id: str
    # decision 由路径决定 (approve / reject)


class MemoryCorrectPayload(BaseModel):
    """提交对记忆的修正（创建新记忆 + 标 old 为 superseded）。"""
    memory_id: str
    idempotency_key: str = ""
    kind: str = "fact"
    subject: str = ""
    predicate: str = ""
    value: str = ""
    scope_type: str = "global"
    scope_id: str = ""
    confidence: float = 1.0
    importance: float = 0.8
    principal_id: str = "owner"


class MemoryDeletePayload(BaseModel):
    memory_id: str
    # 旧语义：soft forget（保留 deprecated，由 erase-memory 取代）。


class ProactiveNegativeFeedbackPayload(BaseModel):
    """主动推送导致的负反馈入口（PLAN-14 R-05）。"""
    idempotency_key: str = ""
    candidate_id: str = ""
    reason: str = "not_relevant"


class DisablePluginPayload(BaseModel):
    name: str  # MCP server name


class PauseConnectorPayload(BaseModel):
    connector_id: str
    paused: bool = True


class ReplayDeliveryPayload(BaseModel):
    delivery_id: str


class DeleteSessionPayload(BaseModel):
    session_id: str


class DeleteSessionsByConvPayload(BaseModel):
    conversation_id: str


# ── Query 响应（必要时） ───────────────────────────────────────


class Pagination(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[dict[str, Any]]


# ── Dashboard DTO ─────────────────────────────────────────────


class DashboardSummary(BaseModel):
    """GET /api/dashboard/summary 响应。"""
    schema_version: str = "1"
    generated_at: str = ""
    profile: str = ""
    readiness: str = "ready"
    readiness_reasons: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    usage_24h: dict[str, Any] = Field(default_factory=dict)
    proactive: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    worker: dict[str, Any] = Field(default_factory=dict)


class AttentionItem(BaseModel):
    """单个待办事项。"""
    kind: str
    severity: str = "info"
    label: str
    target: str | None = None
    target_route: str | None = None
    count: int | None = None


class ComponentHealth(BaseModel):
    """单个组件健康状态。"""
    name: str
    status: str = "ok"
    detail: str | None = None
    latency_ms: int | None = None


class HealthComponents(BaseModel):
    """GET /api/health/components 响应。"""
    schema_version: str = "1"
    generated_at: str = ""
    overall: str = "healthy"
    components: list[ComponentHealth] = Field(default_factory=list)


# ── Plan 08 Dashboard: 新增 Command Payload ──


class BaseCommandPayload(BaseModel):
    """命令基类：统一幂等键 + 预期版本（APPROVAL-COMMANDS §2）。"""
    idempotency_key: str = ""
    expected_version: int | None = None


class MemoryConfirmPayload(BaseCommandPayload):
    """确认记忆候选（PLAN-16 MEM-06：统一 expected_version 乐观锁）。"""
    memory_id: str


class MemoryRejectPayload(BaseCommandPayload):
    """拒绝记忆候选（PLAN-16 MEM-06：统一 expected_version 乐观锁）。"""
    memory_id: str


class MemoryErasePayload(BaseCommandPayload):
    """擦除记忆（PLAN-16 M3 MEM-05）：最小 tombstone + Receipt + Audit。"""
    memory_id: str
    reason: str = "user_request"


class ReviewProactiveCandidatePayload(BaseCommandPayload):
    candidate_id: str
    action: str  # approve_send | digest | dismiss


class UpdateProactivePolicyPayload(BaseCommandPayload):
    energy_value: float | None = None
    dry_run: bool | None = None
    max_pushes_per_hour: int | None = None
    max_pushes_per_day: int | None = None


class ReplayEventPayload(BaseCommandPayload):
    event_id: str


class ReconcileReceiptPayload(BaseCommandPayload):
    receipt_id: str


class DisableToolPayload(BaseCommandPayload):
    tool_name: str


class CreateBackupPayload(BaseCommandPayload):
    pass


class VerifyBackupPayload(BaseCommandPayload):
    backup_id: str


class RestoreBackupPayload(BaseCommandPayload):
    backup_id: str


class ConfigDryRunPayload(BaseCommandPayload):
    content: str


class RollbackConfigPayload(BaseCommandPayload):
    version_id: str


class ReconcileDeliveryPayload(BaseCommandPayload):
    delivery_id: str


class ImportProactiveContextPayload(BaseCommandPayload):
    content: str  # PROACTIVE_CONTEXT.md 的新内容


class RebuildProactiveContextPayload(BaseCommandPayload):
    pass


class ForceConnectorPollPayload(BaseCommandPayload):
    connector_id: str


class ArchiveSkillPayload(BaseCommandPayload):
    skill_id: str


class RestoreSkillPayload(BaseCommandPayload):
    skill_id: str


class PinSkillPayload(BaseCommandPayload):
    skill_id: str
    pinned: bool = True


class PayloadGcDryRunPayload(BaseCommandPayload):
    pass


# ── PLAN-14 Knowledge 命令 ────────────────────────────────────


class KnowledgeRegisterPayload(BaseCommandPayload):
    """注册知识资源并可选立即摄入。"""
    source_uri_hash: str = ""
    source_kind: str = "explicit_local_file"
    media_type: str = "text/markdown"
    principal_id: str = "owner"
    trust_label: str = "unverified"
    content: str = ""  # 非空时立即 ingest
    source_version: str = ""


class KnowledgeRefreshPayload(BaseCommandPayload):
    """刷新知识来源（重新 ingest，content_hash 变化才执行）。"""
    source_uri_hash: str = ""
    principal_id: str = "owner"
    content: str = ""  # 最新文本


class KnowledgeInvalidatePayload(BaseCommandPayload):
    """失效知识资源（撤销检索，重置为 stale）。"""
    resource_id: str
    reason: str = "manual_invalidate"


class KnowledgeErasePayload(BaseCommandPayload):
    """擦除知识资源（撤销检索 + 清理 MemorySource）。"""
    resource_id: str
    reason: str = "manual_erase"
