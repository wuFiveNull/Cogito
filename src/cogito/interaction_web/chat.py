"""interaction-web 聊天路由 —— 让 Web 仪表盘作为真正的 Channel 接入 Core 主链路。

- ``POST /api/chat/send``：向 Agent 提交一条用户消息（构造 web ChannelEnvelope 进主链路）。
- ``WS /api/chat/ws``：浏览器 WebSocket；实时收发消息。收到的消息进主链路，
  Agent 回复经 WebChannelAdapter 队列实时推回。

两条入口都复用与 QQ / Terminal 完全相同的 Core 主链路（InboundService → AgentRunner
→ ChannelGateway → WebChannelAdapter），Core 零改动。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.interaction_web.deps import get_runtime

router = APIRouter()


# ── 请求模型 ───────────────────────────────────────────────────────────────


class ChatSendRequest(BaseModel):
    text: str
    conversation_id: str | None = None
    sender: str = "web-user"


# ── 依赖 ─────────────────────────────────────────────────────────────────


def _build_web_envelope(text: str, conversation_id: str, sender: str) -> ChannelEnvelope:
    """构造一条 web ChannelEnvelope。

    reply_route.channel_instance_id="web" 让 Agent 回复经 ChannelGateway 路由回
    WebChannelAdapter；platform_conversation_id 作为 WS 订阅键。
    """
    return ChannelEnvelope(
        channel_type="web",
        channel_instance_id="web",
        platform_sender_id=sender,
        platform_conversation_id=conversation_id,
        content_parts=[{"content_type": "text", "inline_data": text}],
        reply_route=ReplyRoute(
            channel_instance_id="web",
            platform_conversation_id=conversation_id,
        ),
        received_at=datetime.now(UTC).isoformat(),
        trust_label="authenticated",
    )


# ── POST /api/chat/send (M1) ─────────────────────────────────────────────


@router.post("/api/chat/send")
async def chat_send(
    req: ChatSendRequest,
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """向 Agent 提交一条用户消息，返回本轮 turn/conversation 标识。

    实际回复由后台 worker 异步产出；客户端应通过 WebSocket 接收实时推送，
    或轮询会话历史接口查看结果。
    """
    conversation_id = req.conversation_id or f"web:{uuid.uuid4().hex[:12]}"
    envelope = _build_web_envelope(req.text, conversation_id, req.sender)
    result = runtime.inbound.accept(envelope)
    return {
        "message_id": result.message_id,
        "turn_id": result.turn_id,
        "conversation_id": conversation_id,
        "is_new": result.is_new,
    }


# ── WS /api/chat/ws (M2) ─────────────────────────────────────────────────


@router.websocket("/api/chat/ws")
async def chat_ws(websocket: WebSocket) -> None:
    """浏览器聊天 WebSocket。

    握手后首帧 JSON: ``{"conversation_id": "..."}``（缺省则服务端生成）。
    之后双向 JSON：浏览器 → ``{"text": "..."}``；服务端 → ``{"type":"assistant", ...}``。
    """
    await websocket.accept()
    runtime = getattr(websocket.app.state, "runtime", None)
    if runtime is None or runtime.web_channel_adapter is None:
        await websocket.send_json({"type": "error", "text": "runtime unavailable"})
        await websocket.close()
        return

    adapter = runtime.web_channel_adapter

    try:
        init = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    conversation_id = init.get("conversation_id") or f"web:{uuid.uuid4().hex[:12]}"
    queue = adapter.subscribe(conversation_id)
    await websocket.send_json({"type": "ready", "conversation_id": conversation_id})

    consumer = asyncio.create_task(_ws_consume(websocket, runtime, conversation_id))
    producer = asyncio.create_task(_ws_produce(websocket, queue))
    try:
        done, pending = await asyncio.wait(
            {consumer, producer},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            if task.exception():
                pass  # 异常已在各自协程内处理 / 关闭连接
    finally:
        adapter.unsubscribe(conversation_id)
        try:
            await websocket.close()
        except Exception:
            pass


async def _ws_produce(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    """从 WebChannelAdapter 队列取出 Agent 回复并推回浏览器。

    队列项携带 ``kind`` 区分三类事件：
    - ``send``：新消息（流式首帧为占位符 "…"，非流式为最终全文）
    - ``edit``：流式增量编辑（replace 全量文本，is_final 表示本轮回定稿）
    - ``delete``：流式撤回（取消/失败时删除占位消息）
    """
    while True:
        item = await queue.get()
        kind = item.get("kind", "send")
        conversation_id = item.get("conversation_id")
        try:
            if kind == "edit":
                await websocket.send_json(
                    {
                        "type": "assistant.delta",
                        "conversation_id": conversation_id,
                        "message_id": item.get("platform_message_id"),
                        "text": item.get("text", ""),
                        "operation_seq": item.get("operation_seq", 0),
                        "final": bool(item.get("is_final", False)),
                        "delivery_id": item.get("delivery_id"),
                    }
                )
            elif kind == "delete":
                await websocket.send_json(
                    {
                        "type": "assistant.delete",
                        "conversation_id": conversation_id,
                        "message_id": item.get("platform_message_id"),
                        "reason": item.get("reason", "withdrawn"),
                    }
                )
            else:
                # send：占位符 "…" 表示流式开始；其余视为最终全文
                text = item.get("text", "")
                is_placeholder = text == "…"
                await websocket.send_json(
                    {
                        "type": "assistant",
                        "conversation_id": conversation_id,
                        "message_id": item.get("platform_message_id"),
                        "text": text,
                        "streaming": is_placeholder,
                        "final": not is_placeholder,
                        "delivery_id": item.get("delivery_id"),
                        "reply_to_message_id": item.get("reply_to_message_id"),
                    }
                )
        except Exception:
            return


async def _ws_consume(
    websocket: WebSocket,
    runtime: Any,
    conversation_id: str,
) -> None:
    """接收浏览器消息，构造 web ChannelEnvelope 进主链路。"""
    while True:
        try:
            data = await websocket.receive_json()
        except (WebSocketDisconnect, Exception):
            return
        if not isinstance(data, dict):
            return
        text = data.get("text")
        if not text or not str(text).strip():
            continue
        envelope = _build_web_envelope(
            str(text),
            conversation_id,
            data.get("sender") or "web-user",
        )
        runtime.inbound.accept(envelope)
