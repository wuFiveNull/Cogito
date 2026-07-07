"""ChannelGateway — Gateway Protocol 实现，连接 DeliveryWorker 到 ChannelManager。

数据流:
Delivery (DB) → DeliveryWorker.lease_next() → deliver()
  → ChannelGateway.send(target_snapshot, content_ref)
  → ChannelManager.get(adapter_id).send(conversation_id, text)
  → Platform API 发送消息

QQ-ONEBOT-E2E-01 / PR 2:
- 使用结构化 ChannelSendRequest/ChannelSendResult
- 返回真实 platform_message_id（不再 fake_）
- 区分 temporary/permanent/unknown
- 避免同 event loop 死锁：DeliveryWorker 通过 asyncio.to_thread 调用，
  Gateway 在工作线程内使用 run_coroutine_threadsafe 回到主 loop
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

from cogito.channel.base import ChannelSendRequest, ChannelSendResult
from cogito.channel.manager import ChannelManager
from cogito.service.delivery_worker import Gateway

_LOG = logging.getLogger("cogito.channel.gateway")


class ChannelGateway(Gateway):
    """消息发送通道 —— 将 Delivery 转发到 Channel Adapter。

    解析 target_snapshot JSON 获取 adapter_id 和 conversation_id，
    读取 content_ref 获取消息文本，通过 ChannelManager 调用 Adapter.send()。
    """

    def __init__(self, conn: sqlite3.Connection, channel_manager: ChannelManager) -> None:
        self._conn = conn
        self._channel_manager = channel_manager
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """延迟获取主 loop —— 必须在 RuntimeApplication 启动后调用。"""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def send(self, target_snapshot: str, content_ref: str) -> bool | None:
        """遗留 bool|None 接口 —— 委托 send_request() 后映射。"""
        result = self.send_request(target_snapshot, content_ref)
        if result.status == "sent":
            return True
        if result.status in ("temporary", "permanent"):
            return False
        return None  # unknown

    def send_request(self, target_snapshot: str, content_ref: str) -> ChannelSendResult:
        """结构化发送 —— 返回 ChannelSendResult。

        由 DeliveryWorker 通过 asyncio.to_thread() 调用。
        在工作线程内使用 run_coroutine_threadsafe 回到主 loop 调用 Adapter。
        """
        try:
            target = json.loads(target_snapshot) if isinstance(target_snapshot, str) else target_snapshot
        except (json.JSONDecodeError, TypeError):
            return ChannelSendResult(
                status="permanent",
                error_code="invalid_target_snapshot",
            )

        adapter_id = target.get("adapter_id")
        conversation_id = target.get("conversation_id") or target.get("target")
        reply_route = target.get("reply_route", {})
        if not adapter_id and reply_route:
            adapter_id = reply_route.get("adapter_id") or reply_route.get("channel_instance_id")
        if not conversation_id and reply_route:
            conversation_id = reply_route.get("conversation_id") or reply_route.get("platform_conversation_id")

        if not adapter_id:
            return ChannelSendResult(
                status="permanent",
                error_code="missing_adapter_id",
            )
        if not conversation_id:
            return ChannelSendResult(
                status="permanent",
                error_code="missing_conversation_id",
            )

        # 读取消息内容
        text = self._read_message_text(content_ref)
        if text is None:
            return ChannelSendResult(
                status="permanent",
                error_code="content_not_found",
            )

        # 获取 Adapter
        adapter = self._channel_manager.get_adapter(adapter_id)
        if adapter is None:
            return ChannelSendResult(
                status="temporary",
                error_code="adapter_not_running",
            )

        # 构建结构化请求
        delivery_id = target.get("delivery_id", "")
        attempt_id = target.get("attempt_id", "")
        request = ChannelSendRequest(
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            idempotency_key=target.get("idempotency_key", f"delivery_{delivery_id}"),
            channel_instance_id=adapter_id,
            target_endpoint_ref=target.get("target_endpoint_ref", ""),
            platform_conversation_id=str(conversation_id),
            reply_to_platform_message_id=reply_route.get("reply_to_platform_message_id"),
            text=text,
        )

        # 在主 loop 调用 Adapter
        try:
            loop = self._ensure_loop()
            coro = adapter.send_request(request)
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            result = future.result(timeout=30)
            return result
        except asyncio.CancelledError:
            return ChannelSendResult(status="temporary", error_code="cancelled")
        except TimeoutError:
            return ChannelSendResult(status="unknown", error_code="timeout")
        except ConnectionError:
            return ChannelSendResult(
                status="temporary",
                error_code="connection_error",
            )
        except Exception as e:
            _LOG.exception("ChannelGateway.send_request failed: %s", e)
            # 无法判断外部结果 → unknown
            return ChannelSendResult(
                status="unknown",
                error_code=type(e).__name__,
            )

    def _read_message_text(self, content_ref: str) -> str | None:
        """从 content_ref (message_id) 读取消息文本。"""
        if not content_ref:
            return ""
        row = self._conn.execute(
            "SELECT cp.inline_data FROM content_parts cp "
            "WHERE cp.message_id=? AND cp.content_type='text' "
            "LIMIT 1",
            (content_ref,),
        ).fetchone()
        return row["inline_data"] if row else ""
