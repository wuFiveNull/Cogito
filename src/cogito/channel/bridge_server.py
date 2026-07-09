"""LangBot Bridge Server（Plan 05 M2）。

提供 Gateway ↔ Agent Core 之间的版本化 HTTP 契约：

入站：
  POST /bridge/v1/inbound    → InboundMessage → InboundService

出站（Core → Gateway）：
  POST /bridge/v1/delivery/send             → DeliveryOperation action=send
  POST /bridge/v1/delivery/placeholder      → DeliveryOperation action=start_placeholder
  POST /bridge/v1/delivery/edit             → DeliveryOperation action=append_or_replace
  POST /bridge/v1/delivery/finish           → DeliveryOperation action=finish
  POST /bridge/v1/delivery/delete           → DeliveryOperation action=delete
  POST /bridge/v1/delivery/reconcile        → DeliveryOperation action=reconcile

健康：
  GET  /bridge/v1/health                    → 每个 Instance 的连接/认证/限流/最后事件时间

所有契约模型在 contracts/bridge_dto.py 定义；本模块只做路由薄层。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException

from cogito.contracts.bridge_dto import (
    BridgeError,
    DeliveryOperation,
    decode_inbound,
)

_LOGGER = logging.getLogger("cogito.bridge_server")


# ── 状态聚合 ──


@dataclass
class InstanceHealth:
    """单个 Instance 的健康快照。"""
    instance_id: str
    channel_type: str
    connected: bool = False
    auth_ok: bool = True
    rate_limited: bool = False
    last_event_at: str | None = None
    last_error: str | None = None


@dataclass
class BridgeServer:
    """Bridge Server —— 持有对 InboundService 和 Connection 的引用。

    不直接拥有平台连接（由 Gateway 侧的 Adapter 拥有），
    只负责 DTO 转换 + 路由。
    """

    conn: sqlite3.Connection
    inbound_handler: Any  # Callable[[InboundMessage], Awaitable[str]] — 接受 DTO 返回 message_id
    instance_health: dict[str, InstanceHealth] = field(default_factory=dict)

    def create_router(self) -> APIRouter:
        """创建 /bridge/v1/* 路由组。"""
        router = APIRouter(prefix="/bridge/v1", tags=["bridge"])

        @router.post("/inbound")
        async def post_inbound(payload: dict[str, Any]) -> dict[str, Any]:
            """入站消息Gateway → Core。"""
            try:
                # 自动检测版本并解码
                msg = decode_inbound(payload)
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

            # 幂等：重复 event_id 返回已有结果
            existing = self._check_idempotent(msg.event_id)
            if existing:
                return {"status": "duplicate", "message_id": existing}

            # 调用入站处理器
            try:
                message_id = await self.inbound_handler(msg)
                return {"status": "accepted", "message_id": message_id}
            except Exception as e:
                _LOGGER.exception("Bridge inbound handler failed")
                raise HTTPException(status_code=500, detail=str(e))

        @router.post("/delivery/send")
        def post_delivery_send(payload: dict[str, Any]) -> dict[str, Any]:
            """出站投递：send / placeholder / edit / finish / delete / reconcile。"""
            return self._handle_delivery(payload)

        @router.post("/delivery/placeholder")
        def post_delivery_placeholder(payload: dict[str, Any]) -> dict[str, Any]:
            payload["action"] = "start_placeholder"
            return self._handle_delivery(payload)

        @router.post("/delivery/edit")
        def post_delivery_edit(payload: dict[str, Any]) -> dict[str, Any]:
            payload["action"] = "append_or_replace"
            return self._handle_delivery(payload)

        @router.post("/delivery/finish")
        def post_delivery_finish(payload: dict[str, Any]) -> dict[str, Any]:
            payload["action"] = "finish"
            return self._handle_delivery(payload)

        @router.post("/delivery/delete")
        def post_delivery_delete(payload: dict[str, Any]) -> dict[str, Any]:
            payload["action"] = "delete"
            return self._handle_delivery(payload)

        @router.post("/delivery/reconcile")
        def post_delivery_reconcile(payload: dict[str, Any]) -> dict[str, Any]:
            payload["action"] = "reconcile"
            return self._handle_delivery(payload)

        @router.get("/health")
        def get_health() -> dict[str, Any]:
            """健康接口：报告每个 Instance 的连接/认证/限流/最后事件时间。"""
            instances = []
            for inst in self.instance_health.values():
                instances.append({
                    "instance_id": inst.instance_id,
                    "channel_type": inst.channel_type,
                    "connected": inst.connected,
                    "auth_ok": inst.auth_ok,
                    "rate_limited": inst.rate_limited,
                    "last_event_at": inst.last_event_at,
                    "last_error": inst.last_error,
                })
            values = list(self.instance_health.values())
            overall_healthy = all(i.connected and i.auth_ok for i in values) if values else True
            return {
                "status": "healthy" if overall_healthy else "degraded",
                "instances": instances,
            }

        return router

    def _handle_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理出站 DeliveryOperation。"""
        try:
            op = DeliveryOperation.from_json(payload)
        except Exception as e:
            error = BridgeError(error_code="validation", message=str(e))
            raise HTTPException(status_code=400, detail=error.to_json())

        # 检查 reply route 过期
        # （简化：此处仅校验 action 合法性，实际路由过期检查在 Core 侧）
        if op.action not in ("send", "start_placeholder", "append_or_replace",
                             "finish", "delete", "reconcile"):
            error = BridgeError(error_code="unsupported", message=f"unknown action: {op.action}")
            raise HTTPException(status_code=400, detail=error.to_json())

        # TODO: 实际出站投递逻辑（通过 DeliveryService 创建 Delivery）
        # 当前返回确认；Core 侧通过调用此接口触发投递
        _LOGGER.info(
            "Bridge delivery: action=%s delivery_id=%s attempt_id=%s",
            op.action, op.delivery_id, op.attempt_id,
        )
        return {
            "status": "accepted",
            "operation_id": op.operation_id or uuid.uuid4().hex,
            "action": op.action,
        }

    def _check_idempotent(self, event_id: str) -> str | None:
        """幂等检查：event_id 是否已处理。"""
        if not event_id:
            return None
        try:
            row = self._conn.execute(
                "SELECT message_id FROM inbound_inbox "
                "WHERE platform_event_id=? AND status='processed'",
                (event_id,),
            ).fetchone()
            return row["message_id"] if row else None
        except Exception:
            return None

    def update_health(
        self,
        instance_id: str,
        channel_type: str,
        *,
        connected: bool | None = None,
        auth_ok: bool | None = None,
        rate_limited: bool | None = None,
        last_event_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """更新 Instance 健康状态（由 Gateway 调用）。"""
        h = self.instance_health.setdefault(
            instance_id,
            InstanceHealth(instance_id=instance_id, channel_type=channel_type),
        )
        if connected is not None:
            h.connected = connected
        if auth_ok is not None:
            h.auth_ok = auth_ok
        if rate_limited is not None:
            h.rate_limited = rate_limited
        if last_event_at is not None:
            h.last_event_at = last_event_at
        if last_error is not None:
            h.last_error = last_error
