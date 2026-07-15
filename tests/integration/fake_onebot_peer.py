"""Fake OneBot 11 reverse WebSocket server —— loopback 协议边界。

QQ-ONEBOT-E2E-01 / PR 4:
模拟 NapCat/Lagrange 的 OneBot 11 reverse WS server。
- 客户端 (Cogito) 连接上来后，可推送事件 (message / meta_event)
- 接收 Core 发出的 action (send_private_msg / send_group_msg)
- 返回确定性 message_id
- 可注入故障：断线、响应丢失、临时失败

不改 Cogito 入站路径；从协议边界驱动 Inbound。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import websockets
from websockets.server import serve, WebSocketServerProtocol

_LOG = logging.getLogger("cogito.test.fake_onebot_peer")


class FakeOneBotPeer:
    """Fake OneBot 11 Reverse WebSocket Server。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        access_token: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._access_token = access_token
        self._server: Any = None
        self._ws: WebSocketServerProtocol | None = None
        self._ready = asyncio.Event()
        self._events: asyncio.Queue[dict] = asyncio.Queue()
        self._action_log: list[dict] = []
        self._next_message_id: int = 200001
        self._fail_next_send: str = ""  # "temporary" | "permanent" | "timeout" | ""
        self._drop_response: bool = False
        self._disconnect_on_event: bool = False
        self._running = False

    @property
    def port(self) -> int:
        return self._port

    @property
    def ws_url(self) -> str:
        return f"ws://{self._host}:{self._port}/"

    @property
    def action_log(self) -> list[dict]:
        return list(self._action_log)

    def set_fail_next(self, mode: str) -> None:
        """注入下一次发送故障模式。"""
        self._fail_next_send = mode

    def set_drop_response(self, drop: bool = True) -> None:
        """模拟响应丢失。"""
        self._drop_response = drop

    def set_disconnect_on_event(self, disconnect: bool = True) -> None:
        """模拟收到 event 后断线。"""
        self._disconnect_on_event = disconnect

    async def start(self) -> None:
        """启动 fake peer server。"""

        async def handler(ws: WebSocketServerProtocol) -> None:
            await self._handle(ws)

        self._server = await serve(
            handler,
            self._host,
            self._port,
        )
        # 获取实际 port
        self._port = self._server.sockets[0].getsockname()[1]
        self._running = True
        _LOG.info("FakeOneBotPeer listening on %s", self.ws_url)

    async def wait_ready(self, timeout: float = 5.0) -> None:
        """等待客户端连接上来。"""
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def stop(self) -> None:
        """停止 server。"""
        self._running = False
        if self._ws is not None:
            await self._ws.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def push_message_event(
        self,
        *,
        message_id: int,
        user_id: int,
        text: str,
        group_id: int | None = None,
        self_id: int = 99999999,
        at_bot: bool = False,
    ) -> None:
        """推送一条 message 事件。"""
        if group_id is not None:
            msg_seg: list[dict] = []
            if at_bot:
                msg_seg.append({"type": "at", "data": {"qq": str(self_id)}})
                msg_seg.append({"type": "text", "data": {"text": f" {text}"}})
            else:
                msg_seg.append({"type": "text", "data": {"text": text}})
            payload = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": message_id,
                "group_id": group_id,
                "user_id": user_id,
                "message": msg_seg,
                "raw_message": text,
                "sender": {
                    "user_id": user_id,
                    "nickname": "TestUser",
                    "role": "member",
                },
                "time": 1700000000,
                "self_id": self_id,
            }
        else:
            payload = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": message_id,
                "user_id": user_id,
                "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text,
                "sender": {"user_id": user_id, "nickname": "TestUser"},
                "time": 1700000000,
                "self_id": self_id,
            }

        if self._disconnect_on_event:
            # 模拟在事件推送后立即断线
            if self._ws is not None:
                await self._ws.close()
            return

        if self._ws is not None:
            await self._ws.send(json.dumps(payload))

    async def _handle(self, ws: WebSocketServerProtocol) -> None:
        """处理 Cogito 客户端连接。"""
        self._ws = ws
        self._ready.set()
        _LOG.info("Cogito connected to FakeOneBotPeer")

        try:
            async for raw in ws:
                await self._handle_action(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._ws = None
            _LOG.info("Cogito disconnected")

    async def _handle_action(self, action: dict) -> None:
        """处理 Core 推送的 action (send_private_msg / send_group_msg)。"""
        self._action_log.append(action)

        # 注入故障
        if self._fail_next_send == "timeout":
            self._fail_next_send = ""
            return  # 不响应（模拟 timeout）

        if self._fail_next_send == "temporary":
            self._fail_next_send = ""
            resp = {
                "status": "failed",
                "retcode": 1404,
                "data": None,
                "message": "send failed",
                "wording": "发送失败",
                "msg": "send_failed",
            }
            if self._ws is not None:
                await self._ws.send(json.dumps(resp))
            return

        if self._fail_next_send == "permanent":
            self._fail_next_send = ""
            resp = {
                "status": "failed",
                "retcode": 1403,
                "data": None,
                "message": "forbidden",
                "wording": "权限不足",
                "msg": "forbidden",
            }
            if self._ws is not None:
                await self._ws.send(json.dumps(resp))
            return

        if self._drop_response:
            return  # 响应丢失

        # 默认成功
        action_name = action.get("action", "")
        params = action.get("params", {})
        message_id = self._next_message_id
        self._next_message_id += 1

        if action_name == "send_private_msg":
            resp = {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": message_id},
                "message": "",
                "wording": "",
                "msg": "",
            }
        elif action_name == "send_group_msg":
            resp = {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": message_id},
                "message": "",
                "wording": "",
                "msg": "",
            }
        else:
            resp = {
                "status": "ok",
                "retcode": 0,
                "data": None,
                "message": "",
                "wording": "",
                "msg": "",
            }

        if self._ws is not None:
            await self._ws.send(json.dumps(resp))
