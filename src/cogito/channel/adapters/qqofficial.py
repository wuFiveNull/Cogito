"""QQ Official Adapter — 从 LangBot v4.10.5 复制。

本地修改：
- 所有 langbot_plugin.* imports → cogito.channel.vendor.langbot.compatibility.*
- langbot.libs → cogito.channel.clients
- 新增 Cogito ChannelAdapter 接口方法 (set_inbound_handler, send)
"""
from __future__ import annotations

import asyncio
import datetime
import time

import pydantic

from cogito.channel.clients.qq_official_api.api import QQOfficialClient
from cogito.channel.clients.qq_official_api.qqofficialevent import QQOfficialEvent
from cogito.channel.utils import image
from cogito.channel.vendor.langbot.compatibility import adapter as abstract_platform_adapter
from cogito.channel.vendor.langbot.compatibility import entities as platform_entities
from cogito.channel.vendor.langbot.compatibility import events as platform_events
from cogito.channel.vendor.langbot.compatibility import message as platform_message
from cogito.channel.vendor.langbot.compatibility.logger import EventLogger
from cogito.inbound.models import InboundHandler


def _is_base64_data(value: str) -> bool:
    """检查字符串是否包含 base64 数据而非 URL。"""
    if not value:
        return False
    if value.startswith("data:"):
        return True
    if value.startswith(("http://", "https://", "/", "./", "../")):
        return False
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9+/=\s]{20,}", value))


class QQOfficialMessageConverter(abstract_platform_adapter.AbstractMessageConverter):
    @staticmethod
    async def yiri2target(message_chain: platform_message.MessageChain):
        content_list = []
        for msg in message_chain:
            if type(msg) is platform_message.Plain:
                content_list.append({"type": "text", "content": msg.text})
            elif type(msg) is platform_message.Image:
                url = msg.url if hasattr(msg, "url") and msg.url else None
                b64 = msg.base64 if hasattr(msg, "base64") and msg.base64 else None
                if url and not b64 and _is_base64_data(url):
                    b64 = url
                    url = None
                content_list.append({"type": "image", "url": url, "base64": b64})
            elif type(msg) is platform_message.Voice:
                url = msg.url if hasattr(msg, "url") and msg.url else None
                b64 = msg.base64 if hasattr(msg, "base64") and msg.base64 else None
                if url and not b64 and _is_base64_data(url):
                    b64 = url
                    url = None
                content_list.append({"type": "voice", "url": url, "base64": b64})
            elif type(msg) is platform_message.File:
                url = msg.url if hasattr(msg, "url") and msg.url else None
                b64 = msg.base64 if hasattr(msg, "base64") and msg.base64 else None
                if url and not b64 and _is_base64_data(url):
                    b64 = url
                    url = None
                content_list.append({"type": "file", "url": url, "base64": b64, "name": msg.name if hasattr(msg, "name") else "file"})
        return content_list

    @staticmethod
    async def target2yiri(message: str, message_id: str, pic_url: str, content_type):
        yiri_msg_list = []
        yiri_msg_list.append(platform_message.Source(id=message_id, time=datetime.datetime.now()))
        if pic_url is not None:
            b64_url = await image.get_qq_official_image_base64(pic_url=pic_url, content_type=content_type)
            yiri_msg_list.append(platform_message.Image(base64=b64_url))
        yiri_msg_list.append(platform_message.Plain(text=message))
        return platform_message.MessageChain(yiri_msg_list)


class QQOfficialEventConverter(abstract_platform_adapter.AbstractEventConverter):
    @staticmethod
    async def yiri2target(event: platform_events.MessageEvent) -> QQOfficialEvent:
        return event.source_platform_object

    @staticmethod
    async def target2yiri(event: QQOfficialEvent):
        yiri_chain = await QQOfficialMessageConverter.target2yiri(
            message=event.content, message_id=event.d_id,
            pic_url=event.attachments, content_type=event.content_type,
        )
        if event.t == "C2C_MESSAGE_CREATE":
            return platform_events.FriendMessage(
                sender=platform_entities.Friend(id=event.user_openid, nickname=event.t, remark=""),
                message_chain=yiri_chain,
                time=int(datetime.datetime.strptime(event.timestamp, "%Y-%m-%dT%H:%M:%S%z").timestamp()) if event.timestamp else 0,
                source_platform_object=event,
            )
        if event.t == "DIRECT_MESSAGE_CREATE":
            return platform_events.FriendMessage(
                sender=platform_entities.Friend(id=event.guild_id, nickname=event.t, remark=""),
                message_chain=yiri_chain, source_platform_object=event,
            )
        if event.t == "GROUP_AT_MESSAGE_CREATE":
            yiri_chain.insert(0, platform_message.At(target="justbot"))
            return platform_events.GroupMessage(
                sender=platform_entities.GroupMember(
                    id=event.group_openid, member_name=event.t, permission="MEMBER",
                    group=platform_entities.Group(id=event.group_openid, name="MEMBER", permission=platform_entities.Permission.Member),
                    special_title="",
                ), message_chain=yiri_chain,
                time=int(datetime.datetime.strptime(event.timestamp, "%Y-%m-%dT%H:%M:%S%z").timestamp()) if event.timestamp else 0,
                source_platform_object=event,
            )
        if event.t == "AT_MESSAGE_CREATE":
            yiri_chain.insert(0, platform_message.At(target="justbot"))
            return platform_events.GroupMessage(
                sender=platform_entities.GroupMember(
                    id=event.channel_id, member_name=event.t, permission="MEMBER",
                    group=platform_entities.Group(id=event.channel_id, name="MEMBER", permission=platform_entities.Permission.Member),
                    special_title="",
                ), message_chain=yiri_chain,
                time=int(datetime.datetime.strptime(event.timestamp, "%Y-%m-%dT%H:%M:%S%z").timestamp()) if event.timestamp else 0,
                source_platform_object=event,
            )


class QQOfficialAdapter(abstract_platform_adapter.AbstractMessagePlatformAdapter):
    """QQ 官方机器人适配器。"""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)

    bot: QQOfficialClient
    config: dict
    bot_account_id: str
    adapter_id: str = ""
    channel_type: str = "qqofficial"
    bot_uuid: str | None = None
    enable_webhook: bool = False
    message_converter: QQOfficialMessageConverter = QQOfficialMessageConverter()
    event_converter: QQOfficialEventConverter = QQOfficialEventConverter()

    # Cogito: 入站消息处理器
    _inbound_handler: InboundHandler | None = None

    def __init__(self, config: dict, logger: abstract_platform_adapter.AbstractEventLogger | None = None):
        enable_webhook = config.get("enable-webhook", False)
        bot = QQOfficialClient(
            app_id=config["appid"], secret=config["secret"],
            token=config["token"], logger=logger or EventLogger("channel.qqofficial"),
            unified_mode=enable_webhook,
        )
        adapter_id = str(config.get("uuid", config.get("appid", "qqofficial")[:8]))
        super().__init__(
            config=config, logger=logger or EventLogger("channel.qqofficial"),
            bot=bot, bot_account_id=config["appid"],
            adapter_id=adapter_id,
        )
        self.enable_webhook = enable_webhook
        self._ws_task: asyncio.Task = None
        self._stream_ctx: dict = {}
        self._stream_ctx_ts: dict[str, float] = {}
        self._fallback_text: dict[str, str] = {}
        self._fallback_text_ts: dict[str, float] = {}

    # ── Cogito ChannelAdapter 接口方法 ──

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        self._inbound_handler = handler

    async def send(self, conversation_id: str, message: str, reply_to_message_id: str | None = None) -> dict:
        platform_message.MessageChain([platform_message.Plain(text=message)])
        await self.bot.send_private_text_msg(conversation_id, message, reply_to_message_id or "")
        return {"platform_message_id": ""}

    async def start(self) -> None:
        await self.run_async()

    async def stop(self) -> None:
        await self.kill()

    # ── 原有 LangBot 方法 ──

    async def reply_message(self, message_source, message: platform_message.MessageChain, quote_origin: bool = False):
        event = await QQOfficialEventConverter.yiri2target(message_source)
        content_list = await QQOfficialMessageConverter.yiri2target(message)
        target_type = target_id = None
        if event.t == "C2C_MESSAGE_CREATE":
            target_type, target_id = "c2c", event.user_openid
        elif event.t == "GROUP_AT_MESSAGE_CREATE":
            target_type, target_id = "group", event.group_openid
        elif event.t == "AT_MESSAGE_CREATE":
            for c in content_list:
                if c["type"] == "text":
                    await self.bot.send_channle_group_text_msg(event.channel_id, c["content"], event.d_id)
            return
        elif event.t == "DIRECT_MESSAGE_CREATE":
            for c in content_list:
                if c["type"] == "text":
                    await self.bot.send_channle_private_text_msg(event.guild_id, c["content"], event.d_id)
            return
        for c in content_list:
            ct = c.get("type", "text")
            if ct == "text":
                if target_type == "c2c":
                    await self.bot.send_private_text_msg(target_id, c["content"], event.d_id)
                elif target_type == "group":
                    await self.bot.send_group_text_msg(target_id, c["content"], event.d_id)
            elif ct == "image":
                await self.bot.send_image_msg(target_type, target_id, file_url=c.get("url"), file_data=c.get("base64"), msg_id=event.d_id)
            elif ct == "voice":
                await self.bot.send_voice_msg(target_type, target_id, file_url=c.get("url"), file_data=c.get("base64"), msg_id=event.d_id)
            elif ct == "file":
                await self.bot.send_file_msg(target_type, target_id, file_url=c.get("url"), file_data=c.get("base64"), file_name=c.get("name", "file"), msg_id=event.d_id)

    async def send_message(self, target_type: str, target_id: str, message: platform_message.MessageChain):
        pass

    def register_listener(self, event_type, callback):
        async def on_message(event: QQOfficialEvent):
            self.bot_account_id = "justbot"
            try:
                return await callback(await self.event_converter.target2yiri(event), self)
            except Exception:
                pass
        if event_type == platform_events.FriendMessage:
            self.bot.on_message("DIRECT_MESSAGE_CREATE")(on_message)
            self.bot.on_message("C2C_MESSAGE_CREATE")(on_message)
        elif event_type == platform_events.GroupMessage:
            self.bot.on_message("GROUP_AT_MESSAGE_CREATE")(on_message)
            self.bot.on_message("AT_MESSAGE_CREATE")(on_message)

    def set_bot_uuid(self, bot_uuid: str):
        self.bot_uuid = bot_uuid

    async def handle_unified_webhook(self, bot_uuid: str, path: str, request):
        return await self.bot.handle_unified_webhook(request)

    async def run_async(self):
        if not self.enable_webhook:
            await self._run_websocket()
        else:
            while True:
                await asyncio.sleep(1)

    async def _run_websocket(self):
        async def on_ready():
            pass
        async def on_event(event_type: str, event_data: dict):
            message_types = {"C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"}
            if event_type not in message_types:
                return
            if not isinstance(event_data, dict):
                return
            payload = {"t": event_type, "d": event_data}
            message_data = await self.bot.get_message(payload)
            if message_data:
                event = QQOfficialEvent.from_payload(message_data)
                await self.bot._handle_message(event)
        async def on_error(error: Exception):
            pass
        self._ws_task = asyncio.create_task(self.bot.connect_gateway_loop(on_event, on_ready, on_error))
        try:
            await self._ws_task
        except asyncio.CancelledError:
            pass

    async def kill(self) -> bool:
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        return True

    _STREAM_CTX_TTL = 300

    async def is_stream_output_supported(self) -> bool:
        return self.config.get("enable-stream-reply", False)

    async def create_message_card(self, message_id: str, event) -> bool:
        source = event.source_platform_object
        if source.t != "C2C_MESSAGE_CREATE":
            return False
        ctx = {"user_openid": source.user_openid, "msg_id": source.d_id, "stream_msg_id": None,
               "msg_seq": 1, "index": 0, "last_update_ts": 0, "accumulated_text": "",
               "sent_length": 0, "session_started": False}
        self._stream_ctx[message_id] = ctx
        self._stream_ctx_ts[message_id] = time.time()
        return True

    async def reply_message_chunk(self, message_source, bot_message, message, quote_origin=False, is_final=False):
        text_parts = [msg.text for msg in message if type(msg) is platform_message.Plain]
        chunk_text = "\n\n".join(text_parts)
        message_id = (bot_message.get("resp_message_id") if isinstance(bot_message, dict) else getattr(bot_message, "resp_message_id", None))
        if not message_id or message_id not in self._stream_ctx:
            if chunk_text:
                self._fallback_text[message_id] = self._fallback_text.get(message_id, "") + chunk_text
                self._fallback_text_ts[message_id] = time.time()
            if is_final:
                full_text = self._fallback_text.pop(message_id, "")
                if full_text:
                    await self.reply_message(message_source, platform_message.MessageChain([platform_message.Plain(text=full_text)]), quote_origin)
            return
        ctx = self._stream_ctx[message_id]
        if chunk_text:
            ctx["accumulated_text"] += chunk_text
        if not ctx["session_started"]:
            if not ctx["accumulated_text"]:
                return
            ctx["session_started"] = True
        content_to_send = ctx["accumulated_text"][ctx["sent_length"]:]
        if not content_to_send and not is_final:
            return
        now_ts = time.time()
        if not is_final and (now_ts - ctx["last_update_ts"]) < 0.5:
            return
        ctx["last_update_ts"] = now_ts
        try:
            resp = await self.bot.send_stream_msg(
                user_openid=ctx["user_openid"], content=content_to_send,
                event_id=ctx["msg_id"], msg_id=ctx["msg_id"], msg_seq=ctx["msg_seq"],
                index=ctx["index"], stream_msg_id=ctx["stream_msg_id"], input_state=10 if is_final else 1,
            )
            if resp and isinstance(resp, dict) and resp.get("id"):
                ctx["stream_msg_id"] = resp["id"]
            ctx["sent_length"] = len(ctx["accumulated_text"])
            ctx["index"] += 1
        except Exception:
            pass
        if is_final:
            self._stream_ctx.pop(message_id, None)

    def unregister_listener(self, event_type, callback):
        super().unregister_listener(event_type, callback)
