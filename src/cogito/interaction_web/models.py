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


# ── Query 响应（必要时） ───────────────────────────────────────


class Pagination(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[dict[str, Any]]
