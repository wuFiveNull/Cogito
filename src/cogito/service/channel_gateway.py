"""ChannelGateway — Gateway Protocol 实现，连接 DeliveryWorker 到 ChannelManager。

数据流:
Delivery (DB) → DeliveryWorker.lease_next() → deliver()
  → ChannelGateway.send(target_snapshot, content_ref)
  → ChannelManager.get(adapter_id).send(conversation_id, text)
  → Platform API 发送消息
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from cogito.channel.manager import ChannelManager
from cogito.service.delivery_worker import Gateway


class ChannelGateway(Gateway):
    """消息发送通道 —— 将 Delivery 转发到 Channel Adapter。

    解析 target_snapshot JSON 获取 adapter_id 和 conversation_id，
    读取 content_ref 获取消息文本，通过 ChannelManager 调用 Adapter.send()。
    """

    def __init__(self, conn: sqlite3.Connection, channel_manager: ChannelManager) -> None:
        self._conn = conn
        self._channel_manager = channel_manager
        self._loop = asyncio.get_running_loop()

    def send(self, target_snapshot: str, content_ref: str) -> bool | None:
        """发送消息到平台。

        Args:
            target_snapshot: Delivery target_snapshot JSON，格式:
                {"adapter_id": "...", "conversation_id": "...", ...}
            content_ref: 消息 ID，用于读取消息文本。

        Returns:
            True=成功, False=失败, None=未知
        """
        try:
            target = json.loads(target_snapshot) if isinstance(target_snapshot, str) else target_snapshot
        except (json.JSONDecodeError, TypeError):
            return False

        adapter_id = target.get("adapter_id")
        conversation_id = target.get("conversation_id") or target.get("target")
        reply_route = target.get("reply_route", {})
        if not adapter_id and reply_route:
            adapter_id = reply_route.get("adapter_id") or reply_route.get("channel_instance_id")
        if not conversation_id and reply_route:
            conversation_id = reply_route.get("conversation_id") or reply_route.get("platform_conversation_id")

        if not adapter_id or not conversation_id:
            return False

        # 读取消息内容
        text = self._read_message_text(content_ref)
        if text is None:
            return False

        # 获取 Adapter 并发送
        adapter = self._channel_manager.get_adapter(adapter_id)
        if adapter is None:
            return False

        try:
            result = asyncio.run_coroutine_threadsafe(
                adapter.send(
                    conversation_id=str(conversation_id),
                    message=text,
                ),
                self._loop,
            )
            response = result.result(timeout=30)
            return bool(response)
        except Exception:
            return False

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
