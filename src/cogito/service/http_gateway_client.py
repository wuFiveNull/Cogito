"""HTTP implementation of the GatewayClient port."""

from __future__ import annotations

import json
from typing import Any

import httpx

from cogito.contracts.bridge_dto import ContentPart, DeliveryOperation, TargetSnapshot
from cogito.service.gateway_client import GatewayResult


class HttpGatewayClient:
    """Call a separately deployed Channel Gateway over Bridge V1 HTTP."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)
        self._owns_client = client is None

    def send(self, target_snapshot: str, content: str, idempotency_key: str) -> GatewayResult:
        return self._operation("send", target_snapshot, content, idempotency_key)

    def start_placeholder(
        self,
        target_snapshot: str,
        content: str,
        idempotency_key: str,
    ) -> GatewayResult:
        return self._operation(
            "start_placeholder",
            target_snapshot,
            content,
            idempotency_key,
        )

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
        return self._operation(
            "finish" if is_final else "append_or_replace",
            target_snapshot,
            content,
            idempotency_key,
            operation_seq=operation_seq,
            platform_message_id=platform_message_id,
        )

    def finish(
        self,
        target_snapshot: str,
        platform_message_id: str,
        content: str,
        operation_seq: int,
        idempotency_key: str,
    ) -> GatewayResult:
        return self.edit(
            target_snapshot,
            platform_message_id,
            content,
            operation_seq,
            idempotency_key,
            is_final=True,
        )

    def delete(
        self,
        target_snapshot: str,
        platform_message_id: str,
        operation_seq: int,
        idempotency_key: str,
    ) -> GatewayResult:
        return self._operation(
            "delete",
            target_snapshot,
            "",
            idempotency_key,
            operation_seq=operation_seq,
            platform_message_id=platform_message_id,
        )

    def reconcile(
        self,
        target_snapshot: str,
        platform_message_id: str | None,
        idempotency_key: str,
    ) -> GatewayResult:
        return self._operation(
            "reconcile",
            target_snapshot,
            "",
            idempotency_key,
            platform_message_id=platform_message_id,
        )

    def health(self) -> dict[str, Any]:
        try:
            response = self._client.get(f"{self._base_url}/bridge/v1/health")
            response.raise_for_status()
            value = response.json()
            return value if isinstance(value, dict) else {"status": "degraded", "instances": []}
        except Exception as exc:
            return {"status": "unavailable", "instances": [], "error_code": type(exc).__name__}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _operation(
        self,
        action: str,
        target_snapshot: str,
        content: str,
        idempotency_key: str,
        *,
        operation_seq: int = 1,
        platform_message_id: str | None = None,
    ) -> GatewayResult:
        target = _decode_target(target_snapshot)
        operation = DeliveryOperation(
            operation_id=idempotency_key,
            delivery_id=str(target.get("delivery_id", "")),
            attempt_id=str(target.get("attempt_id", "")),
            operation_seq=operation_seq,
            idempotency_key=idempotency_key,
            target_snapshot=_target_dto(target),
            action=action,
            content=[ContentPart(type="text", data=content)] if content else [],
            platform_message_id=platform_message_id,
        )
        endpoint = {
            "send": "send",
            "start_placeholder": "placeholder",
            "append_or_replace": "edit",
            "finish": "finish",
            "delete": "delete",
            "reconcile": "reconcile",
        }[action]
        try:
            response = self._client.post(
                f"{self._base_url}/bridge/v1/delivery/{endpoint}",
                json=json.loads(operation.to_json()),
            )
            if response.status_code >= 500:
                return GatewayResult(
                    status="temporary",
                    error_code=f"http_{response.status_code}",
                )
            if response.status_code >= 400:
                return GatewayResult(
                    status="permanent",
                    error_code=f"http_{response.status_code}",
                )
            payload = response.json()
            return GatewayResult(
                status=str(payload.get("status", "unknown")),
                platform_message_id=payload.get("platform_message_id"),
                error_code=payload.get("error_code"),
                retry_after_seconds=payload.get("retry_after_seconds"),
            )
        except httpx.TimeoutException:
            return GatewayResult(status="unknown", error_code="timeout")
        except httpx.HTTPError as exc:
            return GatewayResult(status="temporary", error_code=type(exc).__name__)


def _decode_target(value: str) -> dict[str, Any]:
    try:
        target = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return target if isinstance(target, dict) else {}


def _target_dto(target: dict[str, Any]) -> TargetSnapshot:
    route = target.get("reply_route") or {}
    return TargetSnapshot(
        adapter_id=str(
            target.get("adapter_id")
            or route.get("adapter_id")
            or route.get("channel_instance_id")
            or ""
        ),
        channel_type=str(target.get("channel_type") or target.get("channel") or ""),
        conversation_id=str(
            target.get("conversation_id")
            or target.get("target")
            or route.get("platform_conversation_id")
            or ""
        ),
        endpoint_ref=str(
            target.get("target_endpoint_ref") or route.get("target_endpoint_ref") or ""
        ),
        platform=str(target.get("platform") or ""),
    )


__all__ = ["HttpGatewayClient"]
