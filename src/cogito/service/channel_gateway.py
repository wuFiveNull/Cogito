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
from dataclasses import dataclass

from cogito.channel.base import (
    ChannelAttachment,
    ChannelDeleteRequest,
    ChannelEditRequest,
    ChannelSendRequest,
    ChannelSendResult,
)
from cogito.channel.manager import ChannelManager
from cogito.service.delivery_worker import Gateway

_LOG = logging.getLogger("cogito.channel.gateway")


@dataclass(frozen=True)
class _TextContent:
    """Resolved message content — text plus any image attachments."""

    text: str = ""
    attachments: tuple[ChannelAttachment, ...] = ()


# Alias kept private: today the gateway only ever resolves text + attachments.
_Content = _TextContent


class ChannelGateway(Gateway):
    """消息发送通道 —— 将 Delivery 转发到 Channel Adapter。

    解析 target_snapshot JSON 获取 adapter_id 和 conversation_id，
    读取 content_ref 获取消息文本，通过 ChannelManager 调用 Adapter.send()。
    """

    def __init__(self, conn: sqlite3.Connection, channel_manager: ChannelManager) -> None:
        self._conn = conn
        self._channel_manager = channel_manager
        self._loop: asyncio.AbstractEventLoop | None = None


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

        如果在 running loop 内被同步调用（如测试），直接使用 asyncio.run。
        """
        target, adapter_id, conversation_id = self._resolve_target(target_snapshot)
        if adapter_id is None or conversation_id is None:
            return ChannelSendResult(status="permanent", error_code="missing_target")

        # 读取消息内容（文本 + 可能的图片附件）
        content = self._read_message_content(content_ref)
        if content is None:
            return ChannelSendResult(
                status="permanent",
                error_code="content_not_found",
            )

        adapter = self._channel_manager.get_adapter(adapter_id)
        if adapter is None:
            return ChannelSendResult(status="temporary", error_code="adapter_not_running")

        request = self._build_send_request(
            target, content,
            delivery_id=target.get("delivery_id", ""),
            attempt_id=target.get("attempt_id", ""),
        )
        return self._call_adapter_sync(adapter, request)

    def send_text(self, target_snapshot: str, text: str) -> ChannelSendResult:
        """直接以字面文本发送（流式占位创建用，不经 content_ref 读库）。"""
        target, adapter_id, conversation_id = self._resolve_target(target_snapshot)
        if adapter_id is None or conversation_id is None:
            return ChannelSendResult(status="permanent", error_code="missing_target")
        adapter = self._channel_manager.get_adapter(adapter_id)
        if adapter is None:
            return ChannelSendResult(status="temporary", error_code="adapter_not_running")
        request = self._build_send_request(
            target, _TextContent(text=text),
            delivery_id=target.get("delivery_id", ""),
            attempt_id=target.get("attempt_id", ""),
        )
        return self._call_adapter_sync(adapter, request)

    def edit(
        self,
        target_snapshot: str,
        platform_message_id: str,
        text: str,
        operation_seq: int,
        is_final: bool = False,
    ) -> ChannelSendResult:
        """流式编辑：替换某条已发送平台消息的全量内容。"""
        target, adapter_id, conversation_id = self._resolve_target(target_snapshot)
        if adapter_id is None or conversation_id is None:
            return ChannelSendResult(status="permanent", error_code="missing_target")
        adapter = self._channel_manager.get_adapter(adapter_id)
        if adapter is None:
            return ChannelSendResult(status="temporary", error_code="adapter_not_running")
        if not hasattr(adapter, "edit_request_sync"):
            return ChannelSendResult(status="permanent", error_code="adapter_no_edit_support")

        request = ChannelEditRequest(
            delivery_id=target.get("delivery_id", ""),
            attempt_id=target.get("attempt_id", ""),
            idempotency_key=target.get("idempotency_key", f"delivery_{target.get('delivery_id', '')}"),
            channel_instance_id=adapter_id,
            target_endpoint_ref=target.get("target_endpoint_ref", ""),
            platform_conversation_id=str(conversation_id),
            platform_message_id=platform_message_id,
            text=text,
            operation_seq=operation_seq,
            is_final=is_final,
        )
        return self._call_adapter_edit_sync(adapter, request)

    def delete(
        self,
        target_snapshot: str,
        platform_message_id: str,
        reason: str = "withdrawn",
    ) -> None:
        """流式撤回：删除某条已发送平台消息（取消/失败时使用）。"""
        target, adapter_id, conversation_id = self._resolve_target(target_snapshot)
        if adapter_id is None:
            return
        adapter = self._channel_manager.get_adapter(adapter_id)
        if adapter is None or not hasattr(adapter, "delete_request_sync"):
            return
        request = ChannelDeleteRequest(
            delivery_id=target.get("delivery_id", ""),
            attempt_id=target.get("attempt_id", ""),
            channel_instance_id=adapter_id,
            platform_conversation_id=str(conversation_id or ""),
            platform_message_id=platform_message_id,
            reason=reason,
        )
        try:
            adapter.delete_request_sync(request)
        except Exception as e:
            _LOG.exception("ChannelGateway delete_request_sync failed: %s", e)

    def _resolve_target(self, target_snapshot: str | dict) -> tuple[dict | None, str | None, str | None]:
        """解析 target_snapshot，返回 (target_dict, adapter_id, conversation_id)。"""
        try:
            target = json.loads(target_snapshot) if isinstance(target_snapshot, str) else target_snapshot
        except (json.JSONDecodeError, TypeError):
            return None, None, None
        if not isinstance(target, dict):
            return None, None, None

        adapter_id = target.get("adapter_id")
        conversation_id = target.get("conversation_id") or target.get("target")
        reply_route = target.get("reply_route", {})
        if not adapter_id and reply_route:
            adapter_id = reply_route.get("adapter_id") or reply_route.get("channel_instance_id")
        if not conversation_id and reply_route:
            conversation_id = reply_route.get("conversation_id") or reply_route.get("platform_conversation_id")
        return target, adapter_id, conversation_id

    def _build_send_request(
        self, target: dict, content: _Content, *, delivery_id: str, attempt_id: str,
    ) -> ChannelSendRequest:
        adapter_id = target.get("adapter_id")
        reply_route = target.get("reply_route", {})
        conversation_id = target.get("conversation_id") or target.get("target")
        if not conversation_id and reply_route:
            conversation_id = reply_route.get("conversation_id") or reply_route.get("platform_conversation_id")
        return ChannelSendRequest(
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            idempotency_key=target.get("idempotency_key", f"delivery_{delivery_id}"),
            channel_instance_id=adapter_id or "",
            target_endpoint_ref=target.get("target_endpoint_ref", ""),
            platform_conversation_id=str(conversation_id),
            reply_to_platform_message_id=reply_route.get("reply_to_platform_message_id"),
            text=content.text,
            attachments=content.attachments,
        )

    def _call_adapter_sync(self, adapter, request: ChannelSendRequest) -> ChannelSendResult:
        """在可能没有 running loop 的情况下同步调用 adapter.send_request()。

        优先调用 adapter.send_request_sync()（同步版本），不需要 event loop。
        如果 adapter 只有 async send_request()，通过新建 loop 运行。
        """
        # 优先同步调用
        if hasattr(adapter, "send_request_sync"):
            try:
                return adapter.send_request_sync(request)
            except Exception as e:
                _LOG.exception("ChannelGateway send_request_sync failed: %s", e)
                return ChannelSendResult(status="unknown", error_code=type(e).__name__)

        # 异步回退 — 新建 event loop
        coro = adapter.send_request(request)
        try:
            return asyncio.run(asyncio.wait_for(coro, timeout=30))
        except TimeoutError:
            return ChannelSendResult(status="unknown", error_code="timeout")
        except ConnectionError:
            return ChannelSendResult(status="temporary", error_code="connection_error")
        except Exception as e:
            _LOG.exception("ChannelGateway._call_adapter_sync failed: %s", e)
            return ChannelSendResult(status="unknown", error_code=type(e).__name__)

    def _call_adapter_edit_sync(self, adapter, request: ChannelEditRequest) -> ChannelSendResult:
        """同步调用 adapter.edit_request_sync（编辑降级路径）。"""
        try:
            return adapter.edit_request_sync(request)
        except Exception as e:
            _LOG.exception("ChannelGateway edit_request_sync failed: %s", e)
            return ChannelSendResult(status="unknown", error_code=type(e).__name__)

    def _read_message_content(self, content_ref: str) -> _Content | None:
        """从 content_ref (message_id) 读取内容：文本 + 图片附件（按 ordinal）。"""
        if not content_ref:
            return _TextContent(text="")
        text_row = self._conn.execute(
            "SELECT cp.inline_data FROM content_parts cp "
            "WHERE cp.message_id=? AND cp.content_type IN ('text','markdown') "
            "ORDER BY cp.ordinal ASC LIMIT 1",
            (content_ref,),
        ).fetchone()
        text = text_row["inline_data"] if text_row else ""

        image_rows = self._conn.execute(
            "SELECT cp.payload_ref, cp.metadata FROM content_parts cp "
            "WHERE cp.message_id=? AND (cp.content_type='image' "
            "OR cp.content_type LIKE 'image/%') "
            "ORDER BY cp.ordinal ASC",
            (content_ref,),
        ).fetchall()
        attachments: list[ChannelAttachment] = []
        for r in image_rows:
            if not r["payload_ref"]:
                continue
            meta = json.loads(r["metadata"] or "{}")
            attachments.append(ChannelAttachment(
                payload_ref=r["payload_ref"],
                mime=str(meta.get("mime") or "image/png"),
                name=str(meta.get("name") or meta.get("filename") or ""),
            ))
        return _TextContent(text=text, attachments=tuple(attachments))
