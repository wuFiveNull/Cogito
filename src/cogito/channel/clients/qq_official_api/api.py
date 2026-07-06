"""QQ Official API client — 从 LangBot v4.10.5 复制。

本地修改：
- langbot_plugin.* import → cogito.channel.vendor.langbot.compatibility.*
- 相对 import → cogito.channel.clients.qq_official_api.*
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from cogito.channel.clients.qq_official_api.qqofficialevent import QQOfficialEvent


class QQOfficialClient:
    """QQ 官方机器人 API 客户端。

    支持 WebSocket 网关和 Webhook 两种模式。
    """

    def __init__(self, secret: str, token: str, app_id: str, logger, unified_mode: bool = False):
        self.unified_mode = unified_mode
        self.secret = secret
        self.token = token
        self.app_id = app_id
        self._message_handlers = {}
        self.base_url = "https://api.sgroup.qq.com"
        self.access_token = ""
        self.access_token_expiry_time = None
        self.logger = logger
        self._msg_seq_counter = 0
        self._token_refresh_task: asyncio.Task | None = None

    async def check_access_token(self) -> bool:
        if not self.access_token or await self.is_token_expired():
            return False
        return bool(self.access_token and self.access_token.strip())

    async def get_access_token(self) -> None:
        url = "https://bots.qq.com/app/getAppAccessToken"
        async with httpx.AsyncClient() as client:
            params = {"appId": self.app_id, "clientSecret": self.secret}
            headers = {"content-type": "application/json"}
            response = await client.post(url, json=params, headers=headers)
            if response.status_code != 200:
                raise Exception(f"Failed to get access_token: HTTP {response.status_code} {response.text}")
            response_data = response.json()
            access_token = response_data.get("access_token")
            expires_in = int(response_data.get("expires_in", 7200))
            self.access_token_expiry_time = time.time() + expires_in - 60
            if access_token:
                self.access_token = access_token
            else:
                raise Exception("Failed to get access_token: no access_token in response")

    def on_message(self, msg_type: str):
        """注册消息类型处理器。"""
        def decorator(func):
            if msg_type not in self._message_handlers:
                self._message_handlers[msg_type] = []
            self._message_handlers[msg_type].append(func)
            return func
        return decorator

    async def _handle_message(self, event: QQOfficialEvent) -> None:
        msg_type = event.t
        if msg_type in self._message_handlers:
            for handler in self._message_handlers[msg_type]:
                await handler(event)

    async def get_message(self, msg: dict) -> dict:
        d = msg.get("d", {})
        if not isinstance(d, dict):
            return {}
        message_data = {
            "t": msg.get("t", ""),
            "user_openid": d.get("author", {}).get("user_openid", ""),
            "timestamp": d.get("timestamp", ""),
            "d_author_id": d.get("author", {}).get("id", ""),
            "content": d.get("content", ""),
            "d_id": d.get("id", ""),
            "id": msg.get("id", ""),
            "channel_id": d.get("channel_id", ""),
            "username": d.get("author", {}).get("username", ""),
            "guild_id": d.get("guild_id", ""),
            "member_openid": d.get("author", {}).get("openid", ""),
            "group_openid": d.get("group_openid", ""),
        }
        attachments = d.get("attachments", [])
        image_attachments = [a["url"] for a in attachments if await self.is_image(a)]
        image_attachments_type = [a["content_type"] for a in attachments if await self.is_image(a)]
        if image_attachments:
            message_data["image_attachments"] = image_attachments[0]
            message_data["content_type"] = image_attachments_type[0]
        else:
            message_data["image_attachments"] = None
        return message_data

    @staticmethod
    async def is_image(attachment: dict) -> bool:
        return attachment.get("content_type", "").startswith("image/")

    async def send_private_text_msg(self, user_openid: str, content: str, msg_id: str) -> None:
        if not await self.check_access_token():
            await self.get_access_token()
        url = f"{self.base_url}/v2/users/{user_openid}/messages"
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"QQBot {self.access_token}", "Content-Type": "application/json"}
            data = {"content": content, "msg_type": 0, "msg_id": msg_id}
            response = await client.post(url, headers=headers, json=data)
            if response.status_code != 200:
                raise ValueError(response)

    async def send_group_text_msg(self, group_openid: str, content: str, msg_id: str) -> None:
        if not await self.check_access_token():
            await self.get_access_token()
        url = f"{self.base_url}/v2/groups/{group_openid}/messages"
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"QQBot {self.access_token}", "Content-Type": "application/json"}
            data = {"content": content, "msg_type": 0, "msg_id": msg_id}
            response = await client.post(url, headers=headers, json=data)
            if response.status_code != 200:
                raise Exception(response.text)

    async def send_channle_group_text_msg(self, channel_id: str, content: str, msg_id: str) -> bool:
        if not await self.check_access_token():
            await self.get_access_token()
        url = f"{self.base_url}/channels/{channel_id}/messages"
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"QQBot {self.access_token}", "Content-Type": "application/json"}
            params = {"content": content, "msg_type": 0, "msg_id": msg_id}
            response = await client.post(url, headers=headers, json=params)
            if response.status_code == 200:
                return True
            raise Exception(response)

    async def send_channle_private_text_msg(self, guild_id: str, content: str, msg_id: str) -> bool:
        if not await self.check_access_token():
            await self.get_access_token()
        url = f"{self.base_url}/dms/{guild_id}/messages"
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"QQBot {self.access_token}", "Content-Type": "application/json"}
            params = {"content": content, "msg_type": 0, "msg_id": msg_id}
            response = await client.post(url, headers=headers, json=params)
            if response.status_code == 200:
                return True
            raise Exception(response)

    MEDIA_TYPE_IMAGE = 1
    MEDIA_TYPE_VIDEO = 2
    MEDIA_TYPE_VOICE = 3
    MEDIA_TYPE_FILE = 4

    async def upload_media(self, target_type: str, target_id: str, file_type: int,
                           file_url: str | None = None, file_data: str | None = None,
                           file_name: str | None = None) -> str:
        if not await self.check_access_token():
            await self.get_access_token()
        url = f"{self.base_url}/v2/users/{target_id}/files" if target_type == "c2c" else f"{self.base_url}/v2/groups/{target_id}/files"
        body = {"file_type": file_type, "srv_send_msg": False}
        if file_url:
            body["url"] = file_url
        elif file_data:
            if file_data.startswith("data:"):
                match = re.match(r"^data:[^;]+;base64,(.+)$", file_data, re.DOTALL)
                body["file_data"] = match.group(1) if match else file_data
            else:
                body["file_data"] = file_data
        if file_type == self.MEDIA_TYPE_FILE and file_name:
            body["file_name"] = file_name
        async with httpx.AsyncClient(timeout=120) as client:
            headers = {"Authorization": f"QQBot {self.access_token}", "Content-Type": "application/json"}
            response = await client.post(url, headers=headers, json=body)
            if response.status_code == 200:
                return response.json().get("file_info", "")
            raise Exception(f"Failed to upload media: HTTP {response.status_code}")

    async def _send_media_msg(self, target_type: str, target_id: str, file_info: str,
                              msg_id: str | None = None, content: str | None = None) -> None:
        if not await self.check_access_token():
            await self.get_access_token()
        url = f"{self.base_url}/v2/users/{target_id}/messages" if target_type == "c2c" else f"{self.base_url}/v2/groups/{target_id}/messages"
        self._msg_seq_counter += 1
        body = {"msg_type": 7, "media": {"file_info": file_info}, "msg_seq": self._msg_seq_counter}
        if content:
            body["content"] = content
        if msg_id:
            body["msg_id"] = msg_id
        async with httpx.AsyncClient(timeout=120) as client:
            headers = {"Authorization": f"QQBot {self.access_token}", "Content-Type": "application/json"}
            response = await client.post(url, headers=headers, json=body)
            if response.status_code != 200:
                raise Exception(f"Failed to send media: HTTP {response.status_code}")

    async def send_image_msg(self, target_type: str, target_id: str,
                             file_url: str | None = None, file_data: str | None = None,
                             msg_id: str | None = None, content: str | None = None) -> None:
        file_info = await self.upload_media(target_type, target_id, self.MEDIA_TYPE_IMAGE,
                                            file_url=file_url, file_data=file_data)
        await self._send_media_msg(target_type, target_id, file_info, msg_id, content)

    async def send_voice_msg(self, target_type: str, target_id: str,
                             file_url: str | None = None, file_data: str | None = None,
                             msg_id: str | None = None) -> None:
        file_info = await self.upload_media(target_type, target_id, self.MEDIA_TYPE_VOICE,
                                            file_url=file_url, file_data=file_data)
        await self._send_media_msg(target_type, target_id, file_info, msg_id)

    async def send_file_msg(self, target_type: str, target_id: str,
                            file_url: str | None = None, file_data: str | None = None,
                            file_name: str | None = None, msg_id: str | None = None) -> None:
        file_info = await self.upload_media(target_type, target_id, self.MEDIA_TYPE_FILE,
                                            file_url=file_url, file_data=file_data, file_name=file_name)
        await self._send_media_msg(target_type, target_id, file_info, msg_id)

    async def send_stream_msg(self, user_openid: str, content: str, event_id: str, msg_id: str,
                              msg_seq: int = 1, index: int = 0, stream_msg_id: str | None = None,
                              input_state: int = 1) -> dict:
        if not await self.check_access_token():
            await self.get_access_token()
        url = f"{self.base_url}/v2/users/{user_openid}/stream_messages"
        body = {"input_mode": "replace", "input_state": input_state, "content_type": "markdown",
                "content_raw": content, "event_id": event_id, "msg_id": msg_id,
                "msg_seq": msg_seq, "index": index}
        if stream_msg_id:
            body["stream_msg_id"] = stream_msg_id
        async with httpx.AsyncClient(timeout=120) as client:
            headers = {"Authorization": f"QQBot {self.access_token}", "Content-Type": "application/json"}
            response = await client.post(url, headers=headers, json=body)
            if response.status_code != 200:
                raise Exception(f"Failed to send stream message: HTTP {response.status_code}")
            return response.json()

    async def is_token_expired(self) -> bool:
        if self.access_token_expiry_time is None:
            return True
        return time.time() > self.access_token_expiry_time

    @staticmethod
    async def repeat_seed(bot_secret: str, target_size: int = 32) -> bytes:
        seed = bot_secret
        while len(seed) < target_size:
            seed *= 2
        return seed[:target_size].encode("utf-8")

    async def verify(self, validation_payload: dict) -> dict:
        seed = await self.repeat_seed(self.secret)
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        event_ts = validation_payload.get("event_ts", "")
        plain_token = validation_payload.get("plain_token", "")
        signature = private_key.sign((event_ts + plain_token).encode()).hex()
        return {"plain_token": plain_token, "signature": signature}

    INTENT_GUILDS = 1 << 0
    INTENT_GUILD_MEMBERS = 1 << 1
    INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
    INTENT_DIRECT_MESSAGE = 1 << 12
    INTENT_GROUP_AND_C2C = 1 << 25
    INTENT_INTERACTION = 1 << 26
    FULL_INTENTS = INTENT_GUILDS | INTENT_GUILD_MEMBERS | INTENT_PUBLIC_GUILD_MESSAGES | INTENT_DIRECT_MESSAGE | INTENT_GROUP_AND_C2C | INTENT_INTERACTION

    async def get_gateway_url(self) -> str:
        if not await self.check_access_token():
            await self.get_access_token()
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"QQBot {self.access_token}"}
            response = await client.get(f"{self.base_url}/gateway", headers=headers)
            if response.status_code == 200:
                url = response.json().get("url", "")
                if not url:
                    raise Exception("Gateway URL is empty")
                return url
            raise Exception(f"Failed to get Gateway URL: HTTP {response.status_code}")

    async def _background_token_refresh(self) -> None:
        try:
            while True:
                if self.access_token_expiry_time:
                    remain = self.access_token_expiry_time - time.time()
                    if remain > 120:
                        await asyncio.sleep(remain - 60)
                        continue
                self.access_token = ""
                self.access_token_expiry_time = None
                if await self.check_access_token():
                    await asyncio.sleep(60)
                else:
                    await self.get_access_token()
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    async def connect_gateway(self, on_event, on_ready=None, on_error=None):
        """WebSocket 网关连接，含重连逻辑。"""
        import websockets

        session_id = ""
        last_seq = 0
        reconnect_attempts = 0
        max_reconnect_attempts = 100
        backoff_delays = [1, 2, 5, 10, 30, 60]
        rate_limit_delay = 60

        if self._token_refresh_task and not self._token_refresh_task.done():
            self._token_refresh_task.cancel()
            try:
                await self._token_refresh_task
            except asyncio.CancelledError:
                pass
            self._token_refresh_task = None

        while reconnect_attempts <= max_reconnect_attempts:
            heartbeat_interval = 45000
            should_refresh_token = False
            ws = None
            heartbeat_task = None

            if should_refresh_token:
                self.access_token = ""
                self.access_token_expiry_time = None

            try:
                ws_url = await self.get_gateway_url()
            except Exception as e:
                reconnect_attempts += 1
                delay = rate_limit_delay if "100017" in str(e) or "频率" in str(e) else backoff_delays[min(reconnect_attempts - 1, len(backoff_delays) - 1)]
                await asyncio.sleep(delay)
                continue

            try:
                ws = await websockets.connect(ws_url)
            except Exception:
                reconnect_attempts += 1
                delay = backoff_delays[min(reconnect_attempts - 1, len(backoff_delays) - 1)]
                await asyncio.sleep(delay)
                continue

            try:
                async for raw_msg in ws:
                    try:
                        payload = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue
                    op = payload.get("op")
                    d = payload.get("d", {})
                    s = payload.get("s")
                    t_ = payload.get("t")
                    if not isinstance(d, dict):
                        d = {}

                    if op == 10:  # Hello
                        heartbeat_interval = d.get("heartbeat_interval", 45000)
                        if session_id and last_seq > 0:
                            await ws.send(json.dumps({"op": 6, "d": {"token": f"QQBot {self.access_token}", "session_id": session_id, "seq": last_seq}}))
                        else:
                            await ws.send(json.dumps({"op": 2, "d": {"token": f"QQBot {self.access_token}", "intents": self.FULL_INTENTS, "shard": [0, 1]}}))
                        async def _heartbeat_loop(conn, interval_ms):
                            try:
                                while True:
                                    await asyncio.sleep(interval_ms / 1000.0)
                                    await conn.send(json.dumps({"op": 1, "d": last_seq}))
                            except asyncio.CancelledError:
                                pass
                        heartbeat_task = asyncio.create_task(_heartbeat_loop(ws, heartbeat_interval))
                    elif op == 0:  # Dispatch
                        if s is not None:
                            last_seq = s
                        if t_ == "READY":
                            session_id = d.get("session_id", "")
                            reconnect_attempts = 0
                            if on_ready:
                                result = on_ready()
                                if asyncio.iscoroutine(result):
                                    await result
                            if self._token_refresh_task and not self._token_refresh_task.done():
                                self._token_refresh_task.cancel()
                                try:
                                    await self._token_refresh_task
                                except asyncio.CancelledError:
                                    pass
                            self._token_refresh_task = asyncio.create_task(self._background_token_refresh())
                        elif t_ == "RESUMED":
                            reconnect_attempts = 0
                        else:
                            if on_event:
                                result = on_event(t_, d)
                                if asyncio.iscoroutine(result):
                                    await result
                    elif op == 11:  # Heartbeat ACK
                        pass
                    elif op == 7:  # Reconnect
                        break
                    elif op == 9:  # Invalid Session
                        can_resume = d.get("can_resume", False)
                        if not can_resume:
                            session_id = ""
                            last_seq = 0
                            should_refresh_token = True
                        break
                close_code = ws.close_code if hasattr(ws, "close_code") else None
                if close_code == 4004:
                    should_refresh_token = True
                elif close_code in (4006, 4007, 4009):
                    session_id = ""
                    last_seq = 0
                    should_refresh_token = True
                elif close_code == 4008:
                    reconnect_attempts += 1
                    await asyncio.sleep(rate_limit_delay)
                    continue
                elif close_code in (4914, 4915) and on_error:
                    await on_error(Exception(f"Bot disconnected (close_code={close_code})"))
                    return
                elif close_code == 1000:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            reconnect_attempts += 1
            if reconnect_attempts > max_reconnect_attempts:
                if on_error:
                    await on_error(Exception("Max reconnect attempts reached"))
                return
            delay = backoff_delays[min(reconnect_attempts - 1, len(backoff_delays) - 1)]
            await asyncio.sleep(delay)

    async def connect_gateway_loop(self, on_event, on_ready=None, on_error=None):
        """持续重连的网关循环。"""
        await self.connect_gateway(on_event, on_ready, on_error)
