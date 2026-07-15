"""LoopbackGatewayClient — 合并进程部署的 GatewayClient 实现 (PLAN-10 M4)。

复用现有 ChannelManager + Adapter；delivery 经 service 层完成，
platform 回执经 ChannelAdapter.send 真实发出（或测试时走 fake adapter）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from cogito.service.gateway_client import GatewayResult

_LOGGER = logging.getLogger("cogito.loopback_gateway")


class LoopbackGatewayClient:
    """合并进程 GatewayClient：直接调 ChannelManager → Adapter。

    真实部署中应传入应用组合根里的 ChannelManager；
    测试部署时可注入 FakeGateway。
    """

    def __init__(self, channel_gateway: Any) -> None:
        self._gateway = channel_gateway
        self._manager = getattr(channel_gateway, "_channel_manager", channel_gateway)

    def send(
        self, target_snapshot: str, content: str, idempotency_key: str,
    ) -> GatewayResult:
        """Send resolved text through the in-process ChannelGateway."""
        if hasattr(self._gateway, "send_text"):
            try:
                return _from_channel_result(self._gateway.send_text(target_snapshot, content))
            except Exception as exc:
                _LOGGER.warning("LoopbackGateway send_text failed: %s", exc)
                return GatewayResult(status="temporary", error_code=type(exc).__name__)

        try:
            target = json.loads(target_snapshot)
        except (json.JSONDecodeError, TypeError):
            target = {}

        channel_type = target.get("channel", "unknown")
        adapter_id = target.get("adapter_id") or target.get("channel_instance_id")
        adapter = None
        if self._manager is not None:
            if adapter_id:
                adapter = self._manager.get_adapter(adapter_id)
            if adapter is None:
                adapter = self._manager.get_adapter(channel_type)

        if adapter is None:
            _LOGGER.warning("LoopbackGateway: no adapter for channel=%s", channel_type)
            return GatewayResult(
                status="route_expired",
                error_code="no_adapter",
            )

        try:
            if hasattr(adapter, "send_request"):
                result = adapter.send_request(target, content)
                status = getattr(result, "status", "unknown")
                pmid = getattr(result, "platform_message_id", None)
                err = getattr(result, "error_code", None)
                retry = getattr(result, "retry_after_seconds", None)
                return GatewayResult(
                    status=_map_adapter_status(status, err),
                    platform_message_id=pmid,
                    error_code=err,
                    retry_after_seconds=retry,
                )
            # 遗留 bool|None 形式
            legacy = adapter.send(target, content)
            if legacy is True:
                return GatewayResult(
                    status="success",
                    platform_message_id=f"fake-{content[:12]}",
                )
            if legacy is False:
                return GatewayResult(status="permanent", error_code="legacy_false")
            return GatewayResult(status="unknown", error_code="legacy_none")
        except Exception as e:
            _LOGGER.warning("LoopbackGateway send failed: %s", e)
            return GatewayResult(status="temporary", error_code="exception")

    def start_placeholder(
        self, target_snapshot: str, content: str, idempotency_key: str,
    ) -> GatewayResult:
        return self.send(target_snapshot, content, idempotency_key)

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
        if not hasattr(self._gateway, "edit"):
            return GatewayResult(status="unsupported", error_code="adapter_no_edit_support")
        try:
            result = self._gateway.edit(
                target_snapshot, platform_message_id, content, operation_seq,
                is_final=is_final,
            )
            return _from_channel_result(result)
        except Exception as exc:
            _LOGGER.warning("LoopbackGateway edit failed: %s", exc)
            return GatewayResult(status="unknown", error_code=type(exc).__name__)

    def finish(
        self,
        target_snapshot: str,
        platform_message_id: str,
        content: str,
        operation_seq: int,
        idempotency_key: str,
    ) -> GatewayResult:
        return self.edit(
            target_snapshot, platform_message_id, content, operation_seq,
            idempotency_key, is_final=True,
        )

    def delete(
        self,
        target_snapshot: str,
        platform_message_id: str,
        operation_seq: int,
        idempotency_key: str,
    ) -> GatewayResult:
        if not hasattr(self._gateway, "delete"):
            return GatewayResult(status="unsupported", error_code="adapter_no_delete_support")
        try:
            self._gateway.delete(target_snapshot, platform_message_id)
            return GatewayResult(status="success", platform_message_id=platform_message_id)
        except Exception as exc:
            _LOGGER.warning("LoopbackGateway delete failed: %s", exc)
            return GatewayResult(status="unknown", error_code=type(exc).__name__)

    def reconcile(
        self,
        target_snapshot: str,
        platform_message_id: str | None,
        idempotency_key: str,
    ) -> GatewayResult:
        # Most legacy adapters do not expose platform lookup. A known platform
        # id is durable evidence; otherwise remain unknown for manual review.
        if platform_message_id:
            return GatewayResult(status="success", platform_message_id=platform_message_id)
        return GatewayResult(status="unknown", error_code="reconcile_unsupported")

    def health(self) -> dict[str, Any]:
        adapters = getattr(self._manager, "_adapters", {})
        instances = []
        for name, adapter in adapters.items():
            status = str(getattr(adapter, "status", "unknown"))
            instances.append({
                "instance_id": getattr(adapter, "adapter_id", name),
                "channel_type": getattr(adapter, "channel_type", name),
                "connected": status.endswith("running"),
                "auth_ok": status not in ("error", "AdapterStatus.error"),
                "rate_limited": False,
            })
        return {
            "status": "healthy" if all(i["connected"] for i in instances) else "degraded",
            "instances": instances,
        }


def _map_adapter_status(status: str, error_code: str | None) -> str:
    """把 adapter 状态映射回 GatewayClient 的 GatewayResult.status。"""
    if status == "sent":
        return "success"
    if status == "permanent":
        return "permanent"
    if status == "temporary":
        return "temporary"
    if status == "unknown":
        return "unknown"
    # 兼容 error_code 分类
    if error_code in ("rate_limited", "rate_limit"):
        return "rate_limited"
    if error_code in ("auth_error", "auth_failed", "unauthorized"):
        return "auth_error"
    if error_code in ("route_expired", "route_invalid"):
        return "route_expired"
    if error_code in ("unsupported", "not_implemented"):
        return "unsupported"
    if error_code in ("too_large", "payload_too_large"):
        return "too_large"
    return status if status else "unknown"


def _from_channel_result(result: Any) -> GatewayResult:
    return GatewayResult(
        status=_map_adapter_status(
            str(getattr(result, "status", "unknown")),
            getattr(result, "error_code", None),
        ),
        platform_message_id=getattr(result, "platform_message_id", None),
        error_code=getattr(result, "error_code", None),
        retry_after_seconds=getattr(result, "retry_after_seconds", None),
    )
