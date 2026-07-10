"""LoopbackGatewayClient — 合并进程部署的 GatewayClient 实现 (PLAN-10 M4)。

复用现有 ChannelManager + Adapter；delivery 经 service 层完成，
platform 回执经 ChannelAdapter.send 真实发出（或测试时走 fake adapter）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from cogito.service.sqlite_delivery_service import GatewayResult

_LOGGER = logging.getLogger("cogito.loopback_gateway")


class LoopbackGatewayClient:
    """合并进程 GatewayClient：直接调 ChannelManager → Adapter。

    真实部署中应传入应用组合根里的 ChannelManager；
    测试部署时可注入 FakeGateway。
    """

    def __init__(self, channel_manager: Any) -> None:
        self._manager = channel_manager

    def send(
        self, target_snapshot: str, content_ref: str, idempotency_key: str,
    ) -> GatewayResult:
        """解析 target_snapshot, 经 ChannelManager 找 adapter 并 send_request。"""
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
                result = adapter.send_request(target, content_ref)
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
            legacy = adapter.send(target, content_ref)
            if legacy is True:
                return GatewayResult(
                    status="success",
                    platform_message_id=f"fake-{content_ref[:12]}",
                )
            if legacy is False:
                return GatewayResult(status="permanent", error_code="legacy_false")
            return GatewayResult(status="unknown", error_code="legacy_none")
        except Exception as e:
            _LOGGER.warning("LoopbackGateway send failed: %s", e)
            return GatewayResult(status="temporary", error_code="exception")


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
