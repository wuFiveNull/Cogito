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


class MemoryConfirmPayload(BaseModel):
    memory_id: str


class MemoryDeletePayload(BaseModel):
    memory_id: str


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


class ReviewProactiveCandidatePayload(BaseModel):
    candidate_id: str
    action: str  # approve_send | digest | dismiss


class UpdateProactivePolicyPayload(BaseModel):
    energy_value: float | None = None
    dry_run: bool | None = None
    max_pushes_per_hour: int | None = None
    max_pushes_per_day: int | None = None


class ReplayEventPayload(BaseModel):
    event_id: str


class ReconcileReceiptPayload(BaseModel):
    receipt_id: str


class DisableToolPayload(BaseModel):
    tool_name: str


class CreateBackupPayload(BaseModel):
    pass


class VerifyBackupPayload(BaseModel):
    backup_id: str


class RestoreBackupPayload(BaseModel):
    backup_id: str


class ConfigDryRunPayload(BaseModel):
    content: str


class RollbackConfigPayload(BaseModel):
    version_id: str


class PayloadGcDryRunPayload(BaseModel):
    pass
