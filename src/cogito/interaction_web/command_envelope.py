"""Command Envelope — 统一写 API (Plan 05 M4).command_service.py set_approval_decision 直接执行 SQL。

所有写操作通过 Command Envelope：
- command_id / command_type / actor / idempotency_key / target / expected_version /
  payload / expires / origin / trace
- 唯一约束 (actor, command_type, idempotency_key)
- 重复同 payload 返回首次结果；相同键不同 payload → idempotency_conflict
- 聚合变更必须 expected_version；条件更新实现冲突优先级：终态 > 取消/过期 > 审批 > 重试
- 所有 Command 写 Audit 和 canonical Event log
- loopback 默认；远程 bind 显式拒绝；写请求校验 Origin
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommandEnvelope:
    """统一命令信封 (Plan 05 M4)。"""

    command_id: str = ""
    command_type: str = ""
    actor: str = ""
    idempotency_key: str = ""
    target_type: str = ""
    target_id: str = ""
    expected_version: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None
    origin: str = ""
    trace_id: str = ""

    def __post_init__(self) -> None:
        if not self.command_id:
            import uuid

            object.__setattr__(self, "command_id", uuid.uuid4().hex)


@dataclass(frozen=True)
class CommandResult:
    """命令执行结果。"""

    command_id: str
    status: str  # "ok" | "idempotent" | "conflict" | "error"
    target_id: str = ""
    message: str = ""
    previous_version: int | None = None


class CommandError(ValueError):
    """命令错误分类 (Plan 05 M4)."""

    pass


class IdempotencyConflictError(CommandError):
    """相同键不同 payload。"""

    pass


class VersionConflictError(CommandError):
    """expected_version 不匹配。"""

    pass


# 冲突优先级 (Plan 05 M4): 终态 > 取消/过期 > 审批 > 重试
_CONFLICT_PRIORITY = {
    "completed": 4,
    "cancelled": 3,
    "expired": 3,
    "failed": 3,
    "approved": 2,
    "retrying": 1,
}


def resolve_conflict(current_status: str, incoming_priority: str) -> bool:
    """终态优先。终态 > 取消/过期 > 审批 > 重试。"""
    return _CONFLICT_PRIORITY.get(current_status, 0) >= _CONFLICT_PRIORITY.get(incoming_priority, 0)


def enforce_loopback_only(origin: str) -> None:
    """控制面默认仅 loopback；远程 bind 显式拒绝。"""
    if origin and origin not in ("127.0.0.1", "localhost", "::1", "dashboard"):
        raise CommandError(f"remote bind not allowed: {origin}")
