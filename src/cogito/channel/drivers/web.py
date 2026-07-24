"""WebChannelAdapter —— 浏览器 WebSocket 的 Channel Adapter。

作为 Core 主链路的一个 Channel，与 QQ / Terminal 完全对称：

- 入站：浏览器消息由 interaction_web/chat.py 在 WebSocket 收到后，构造
  ``ChannelEnvelope(channel_type="web")`` 调 ``InboundService.accept`` 进入主链路
  （与 QQ / Terminal 走同一条 Core 入口）。
- 出站：Agent 回复经 ``CanonicalEffectWorker → ChannelGateway → send_request_sync`` 推入本
  adapter 的内存队列，再由 WebSocket 实时推回浏览器。

设计要点
--------
- ``send_request_sync`` 可由 canonical effect worker 同步调用，必须线程安全。
- 主事件循环内的 WebSocket 任务 ``await`` 每个 conversation 的 ``asyncio.Queue`` 收消息。
- 通过 ``loop.call_soon_threadsafe`` 把线程外的消息搬到主循环的 asyncio 队列。
- 连接断开期间的消息进入信箱（mailbox），重连后回灌，避免丢消息。
"""

from __future__ import annotations

import asyncio
import collections
import sqlite3
import threading
from typing import Any

from cogito.channel.base import (
    AdapterStatus,
    ChannelCapabilities,
    ChannelDeleteRequest,
    ChannelEditRequest,
    ChannelSendRequest,
    ChannelSendResult,
)
from cogito.contracts.inbound import InboundHandler


class WebChannelAdapter:
    """浏览器 WebSocket 渠道适配器（Core ChannelAdapter 协议实现）。"""

    def __init__(
        self,
        adapter_id: str = "web",
        channel_type: str = "web",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._channel_type = channel_type
        self._status = AdapterStatus.created
        self._inbound_handler: InboundHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # 只读连接：用于订阅时清理崩溃遗留的流式占位气泡（见 subscribe）
        self._conn = conn

        # 跨线程缓冲：effect worker 线程 → 主事件循环（线程安全）
        self._cross: collections.deque[dict[str, Any]] = collections.deque()
        self._cross_lock = threading.Lock()

        # 主循环内的订阅表：conversation_id -> asyncio.Queue
        self._subscribers: dict[str, asyncio.Queue[dict[str, Any]]] = {}

        # 断线期间信箱：conversation_id -> list[dict]
        self._mailbox: dict[str, list[dict[str, Any]]] = {}
        self._mailbox_lock = threading.Lock()

    # ── ChannelAdapter 协议 ────────────────────────────────────────────────

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def channel_type(self) -> str:
        return self._channel_type

    @property
    def status(self) -> AdapterStatus:
        return self._status

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        self._inbound_handler = handler

    async def start(self) -> None:
        # 在 ChannelManager 启动任务的同一事件循环内被 await，
        # 因此拿到的就是 Web 服务与 Agent worker 共享的主循环。
        self._loop = asyncio.get_running_loop()
        self._status = AdapterStatus.running

    async def stop(self) -> None:
        self._status = AdapterStatus.stopped
        self._subscribers.clear()
        self._mailbox.clear()

    async def send(
        self,
        conversation_id: str,
        message: str,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """遗留 send 接口 —— 委托 send_request()。"""
        request = ChannelSendRequest(
            delivery_id="",
            attempt_id="",
            idempotency_key=f"web_{id(self)}_{reply_to_message_id or ''}",
            channel_instance_id=self._adapter_id,
            target_endpoint_ref="",
            platform_conversation_id=conversation_id,
            reply_to_platform_message_id=reply_to_message_id,
            text=message,
        )
        result = await self.send_request(request)
        return {
            "status": result.status,
            "platform_message_id": result.platform_message_id,
            "error_code": result.error_code,
        }

    async def send_request(self, request: ChannelSendRequest) -> ChannelSendResult:
        # 主循环内调用（测试或同步路径）时，直接入队。
        return self._enqueue(request)

    def send_request_sync(self, request: ChannelSendRequest) -> ChannelSendResult:
        """被 effect worker 同步调用：推入跨线程缓冲并唤醒主循环。"""
        platform_message_id = f"web_{request.delivery_id or 'x'}"
        item = {
            "kind": "send",
            "conversation_id": request.platform_conversation_id,
            "text": request.text,
            "delivery_id": request.delivery_id,
            "platform_message_id": platform_message_id,
            "reply_to_message_id": request.reply_to_platform_message_id,
        }
        self._enqueue_item(item)
        return ChannelSendResult(status="sent", platform_message_id=platform_message_id)

    def edit_request_sync(self, request: ChannelEditRequest) -> ChannelSendResult:
        """流式编辑：推送 edit 事件（含 platform_message_id 与全量文本）。"""
        platform_message_id = request.platform_message_id or f"web-msg-{request.delivery_id}"
        item = {
            "kind": "edit",
            "conversation_id": request.platform_conversation_id,
            "platform_message_id": platform_message_id,
            "text": request.text,
            "operation_seq": request.operation_seq,
            "is_final": request.is_final,
            "delivery_id": request.delivery_id,
        }
        self._enqueue_item(item)
        return ChannelSendResult(status="sent", platform_message_id=platform_message_id)

    def delete_request_sync(self, request: ChannelDeleteRequest) -> None:
        """流式撤回：推送 delete 事件。"""
        item = {
            "kind": "delete",
            "conversation_id": request.platform_conversation_id,
            "platform_message_id": request.platform_message_id,
            "reason": request.reason,
            "delivery_id": request.delivery_id,
        }
        self._enqueue_item(item)

    # ── 内部：入队与跨线程搬运 ─────────────────────────────────────────────

    def _enqueue_item(self, item: dict[str, Any]) -> None:
        """把队列项推入跨线程缓冲并唤醒主循环（线程安全）。"""
        with self._cross_lock:
            self._cross.append(item)

        loop = self._loop
        if loop is not None and loop.is_running():
            # 从 worker 线程唤醒主循环执行 _pump
            loop.call_soon_threadsafe(self._pump)
        else:
            # 没有可用主循环（如脱离 event loop 的单元测试）直接进信箱
            self._stash_mailbox(item)

    def _pump(self) -> None:
        """在主循环内执行：把跨线程缓冲搬到 asyncio 队列 / 信箱。"""
        while True:
            with self._cross_lock:
                if not self._cross:
                    break
                item = self._cross.popleft()
            q = self._subscribers.get(item["conversation_id"])
            if q is not None:
                q.put_nowait(item)
            else:
                self._stash_mailbox(item)

    def _stash_mailbox(self, item: dict[str, Any]) -> None:
        cid = item["conversation_id"]
        with self._mailbox_lock:
            self._mailbox.setdefault(cid, []).append(item)

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_streaming=True,
            supports_edit=True,
            supports_buttons=False,
            supports_threads=False,
            supports_files=False,
            supports_delete=True,
            max_message_length=8000,
        )

    # ── WebSocket 订阅接口（主循环内调用）────────────────────────────────

    def subscribe(self, conversation_id: str) -> asyncio.Queue[dict[str, Any]]:
        """订阅某会话；返回一个 asyncio.Queue，并回灌断线期间的信箱。"""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[conversation_id] = q
        with self._mailbox_lock:
            buffered = self._mailbox.pop(conversation_id, [])
        for item in buffered:
            q.put_nowait(item)

        # 崩溃恢复：回放已取消的流式 Delivery Event，并清理占位气泡。
        # 尚未迁移的历史记录才从 compatibility projection 查询。
        self._reconcile_interrupted(conversation_id, q)
        return q

    def _reconcile_interrupted(
        self, conversation_id: str, q: asyncio.Queue[dict[str, Any]]
    ) -> None:
        """Replay cancelled streaming Events into idempotent delete commands."""
        if self._conn is None:
            return
        try:
            from cogito.store.event_replay import replay_delivery
            from cogito.store.event_store import EventStore

            grouped: dict[str, list[Any]] = {}
            for event in EventStore(self._conn).read_stream_type("delivery"):
                grouped.setdefault(event.stream_id, []).append(event)
            for delivery_id, stream in grouped.items():
                state = replay_delivery(stream, delivery_id)
                if (
                    state is None
                    or state.delivery_mode != "streaming"
                    or state.platform_conversation_id != conversation_id
                    or state.status not in {"cancelled", "failed"}
                    or not state.platform_message_id
                ):
                    continue
                q.put_nowait(
                    {
                        "kind": "delete",
                        "conversation_id": conversation_id,
                        "platform_message_id": state.platform_message_id,
                        "reason": "recovered",
                        "delivery_id": delivery_id,
                    }
                )

        except Exception:
            return

    def unsubscribe(self, conversation_id: str) -> None:
        """取消订阅（WebSocket 断开时调用）。"""
        self._subscribers.pop(conversation_id, None)
