"""GatewayClient port shared by loopback and HTTP deployment shapes.

The port represents platform operations only.  It never creates or mutates a
Core Delivery aggregate; callers persist intent before invoking it and persist
the returned receipt afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class GatewayResult:
    """Safe, transport-neutral platform result."""

    status: str
    platform_message_id: str | None = None
    error_code: str | None = None
    retry_after_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "platform_message_id": self.platform_message_id,
            "error_code": self.error_code,
            "retry_after_seconds": self.retry_after_seconds,
        }


@runtime_checkable
class GatewayClient(Protocol):
    """Version-independent platform-operation port.

    ``target_snapshot`` is the immutable JSON representation captured when the
    Delivery was created. ``content`` is already resolved text; a separated
    Gateway must never read the Core database to dereference ``content_ref``.
    """

    def send(
        self, target_snapshot: str, content: str, idempotency_key: str,
    ) -> GatewayResult:
        ...

    def start_placeholder(
        self, target_snapshot: str, content: str, idempotency_key: str,
    ) -> GatewayResult:
        ...

    def edit(
        self,
        target_snapshot: str,
        platform_message_id: str,
        content: str,
        operation_seq: int,
        idempotency_key: str,
        *,
        is_final: bool = False,
    ) -> GatewayResult:
        ...

    def finish(
        self,
        target_snapshot: str,
        platform_message_id: str,
        content: str,
        operation_seq: int,
        idempotency_key: str,
    ) -> GatewayResult:
        ...

    def delete(
        self,
        target_snapshot: str,
        platform_message_id: str,
        operation_seq: int,
        idempotency_key: str,
    ) -> GatewayResult:
        ...

    def reconcile(
        self,
        target_snapshot: str,
        platform_message_id: str | None,
        idempotency_key: str,
    ) -> GatewayResult:
        ...

    def health(self) -> dict[str, Any]:
        ...


PERMANENT_GATEWAY_STATUSES = frozenset({
    "permanent", "auth_error", "route_expired", "unsupported", "too_large",
})


def gateway_status_to_channel(status: str) -> str:
    """Map Bridge/Gateway taxonomy to DeliveryWorker taxonomy."""
    if status in ("success", "sent"):
        return "sent"
    if status in PERMANENT_GATEWAY_STATUSES:
        return "permanent"
    if status in ("temporary", "rate_limited"):
        return "temporary"
    return "unknown"


__all__ = [
    "GatewayClient",
    "GatewayResult",
    "PERMANENT_GATEWAY_STATUSES",
    "gateway_status_to_channel",
]
