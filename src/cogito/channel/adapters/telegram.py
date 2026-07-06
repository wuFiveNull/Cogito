"""Telegram Adapter — Cogito 原生实现。

从 LangBot 4.10.5 复制并重写为 Cogito ChannelAdapter。
- 直接实现 ChannelAdapter (不依赖 LangBot 抽象类)
- 入站消息直接构造 Cogito Inbound (不经过 LangBot Event类型)
- 保留 yiri2target 用于出站消息转换
"""
from __future__ import annotations

import base64
import typing

import telegram
import telegram.ext
import telegramify_markdown
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from cogito.channel.utils import httpclient
from cogito.channel.vendor.langbot.compatibility import message as platform_message
from cogito.inbound.models import Inbound, InboundContent, InboundHandler, InboundRoute


class TelegramMessageConverter:
    """消息转换器 —— 将 Cogito MessageChain 转换为 Telegram API 格式。

    从 LangBot TelegramMessageConverter 复制。
    仅保留出站方向 (yiri2target)，入站直接构造 Inbound。
    """

    @staticmethod
    async def yiri2target(message_chain: platform_message.MessageChain, bot: telegram.Bot) -> list[dict]:
        """将 MessageChain 转换为 Telegram API 组件列表。"""
        components = []

        for component in message_chain:
            if isinstance(component, platform_message.Plain):
                components.append({'type': 'text', 'text': component.text})
            elif isinstance(component, platform_message.Image):
                photo_bytes = None
                if component.base64:
                    photo_bytes = base64.b64decode(component.base64)
                elif component.url:
                    session = httpclient.get_session()
                    async with session.get(component.url) as response:
                        photo_bytes = await response.read()
                elif component.path:
                    with open(component.path, 'rb') as f:
                        photo_bytes = f.read()
                components.append({'type': 'photo', 'photo': photo_bytes})
            elif isinstance(component, platform_message.File):
                file_bytes = None
                if component.base64:
                    b64_data = component.base64
                    if ';base64,' in b64_data:
                        b64_data = b64_data.split(';base64,', 1)[1]
                    file_bytes = base64.b64decode(b64_data)
                elif component.url:
                    session = httpclient.get_session()
                    async with session.get(component.url) as response:
                        file_bytes = await response.read()
                elif component.path:
                    with open(component.path, 'rb') as f:
                        file_bytes = f.read()
                file_name = getattr(component, 'name', None) or 'file'
                components.append({'type': 'document', 'document': file_bytes, 'filename': file_name})
            elif isinstance(component, platform_message.Forward):
                for node in component.node_list:
                    components.extend(await TelegramMessageConverter.yiri2target(node.message_chain, bot))

        return components


class TelegramAdapter:
    """Telegram 平台适配器。

    从 Telegram Update 直接构造 Cogito Inbound。
    不依赖 LangBot 抽象类、Event 类型或 Entity 类型。
    """

    def __init__(self, config: dict, logger: typing.Any = None) -> None:
        self.adapter_id = str(config.get('uuid', config.get('token', 'telegram')[:8]))
        self.channel_type = 'telegram'
        self.config = config
        self._inbound_handler: InboundHandler | None = None
        self._logger = logger

        self.bot: telegram.Bot | None = None
        self.application: telegram.ext.Application | None = None
        self._msg_stream_id: dict = {}
        self._seq: int = 1

        self._build_application(config)

    def _build_application(self, config: dict) -> None:
        """创建 Telegram Application 并注册消息处理器。"""
        application = ApplicationBuilder().token(config['token']).build()
        self.bot = application.bot

        async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.message and update.message.from_user and update.message.from_user.is_bot:
                return
            await self._on_update(update)

        application.add_handler(
            MessageHandler(
                filters.TEXT | filters.COMMAND | filters.PHOTO | filters.VOICE | filters.Document.ALL,
                callback,
            )
        )
        self.application = application

    # ── Cogito ChannelAdapter 接口 ──

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        """设置入站消息处理器。"""
        self._inbound_handler = handler

    async def start(self) -> None:
        """启动 Telegram Bot polling。"""
        await self.application.initialize()
        self.bot_account_id = (await self.bot.get_me()).username
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await self.application.start()

    async def stop(self) -> None:
        """停止 Telegram Bot。"""
        if self.application and self.application.running:
            await self.application.stop()
            if self.application.updater:
                await self.application.updater.stop()

    async def send(self, conversation_id: str, message: str, reply_to_message_id: str | None = None) -> dict:
        """发送文本消息到 Telegram。"""
        args = {'chat_id': int(conversation_id), 'text': message}
        if reply_to_message_id:
            args['reply_to_message_id'] = int(reply_to_message_id)
        sent = await self.bot.send_message(**args)
        return {'platform_message_id': str(sent.message_id)}

    # ── 入站消息处理 ──

    async def _on_update(self, update: Update) -> None:
        """处理 Telegram Update，直接构造 Cogito Inbound。"""
        if self._inbound_handler is None:
            return

        message = update.message
        if message is None:
            return

        # 提取会话信息
        chat = update.effective_chat
        if chat is None:
            return
        conversation_id = str(chat.id)
        sender_id = str(message.from_user.id) if message.from_user else conversation_id
        message_id = str(message.message_id)
        reply_to_id = str(message.reply_to_message.message_id) if message.reply_to_message else None

        # 提取消息内容
        content = self._extract_content(message)

        # 构造 Inbound
        inbound = Inbound(
            channel='telegram',
            channel_instance_id=self.adapter_id,
            conversation_id=conversation_id,
            sender_id=sender_id,
            message_id=message_id,
            reply_to_message_id=reply_to_id,
            content=content,
            timestamp=int(message.date.timestamp()),
            metadata={'chat_type': chat.type or 'private'},
            route=InboundRoute(
                adapter_id=self.adapter_id,
                channel_type='telegram',
                conversation_id=conversation_id,
                source_message_id=message_id,
                raw={
                    'chat_id': conversation_id,
                    'message_id': message_id,
                    'sender_id': sender_id,
                },
            ),
        )

        await self._inbound_handler(inbound)

    def _extract_content(self, message: telegram.Message) -> list[InboundContent]:
        """从 Telegram Message 提取 InboundContent 列表。"""
        result: list[InboundContent] = []

        if message.text:
            result.append(InboundContent(type='text', data=message.text))

        if message.caption:
            result.append(InboundContent(type='text', data=message.caption))

        if message.photo:
            # 入站图片只记录引用，不立即下载
            result.append(InboundContent(
                type='image',
                data=f'photo:{message.photo[-1].file_id}',
            ))

        if message.voice:
            result.append(InboundContent(
                type='voice',
                data=f'voice:{message.voice.file_id}',
                mime=message.voice.mime_type or 'audio/ogg',
            ))

        if message.document:
            result.append(InboundContent(
                type='file',
                data=f'document:{message.document.file_id}',
                mime=message.document.mime_type or 'application/octet-stream',
                name=message.document.file_name or 'file',
                size=message.document.file_size or 0,
            ))

        return result

    # ── 出站消息 (保留 LangBot 兼容) ──

    async def send_message(self, target_type: str, target_id: str, message: platform_message.MessageChain) -> None:
        """LangBot 兼容：发送 MessageChain 到指定目标。"""
        components = await TelegramMessageConverter.yiri2target(message, self.bot)
        chat_id_str, _, thread_id_str = str(target_id).partition('#')
        chat_id: int | str = int(chat_id_str) if chat_id_str.lstrip('-').isdigit() else chat_id_str
        message_thread_id = int(thread_id_str) if thread_id_str and thread_id_str.isdigit() else None

        for component in components:
            component_type = component.get('type')
            args = {'chat_id': chat_id}
            if message_thread_id is not None:
                args['message_thread_id'] = message_thread_id

            if component_type == 'text':
                text = component.get('text', '')
                if self.config.get('markdown_card', False):
                    text = telegramify_markdown.markdownify(content=text)
                    args['parse_mode'] = 'MarkdownV2'
                args['text'] = text
                await self.bot.send_message(**args)
            elif component_type == 'photo':
                photo = component.get('photo')
                if photo is not None:
                    args['photo'] = telegram.InputFile(photo)
                    await self.bot.send_photo(**args)
            elif component_type == 'document':
                doc = component.get('document')
                if doc is not None:
                    args['document'] = telegram.InputFile(doc, filename=component.get('filename', 'file'))
                    await self.bot.send_document(**args)

    async def reply_message(self, message_source, message: platform_message.MessageChain, quote_origin: bool = False) -> None:
        """LangBot 兼容：回复消息。"""
        if not isinstance(message_source.source_platform_object, Update):
            return
        update = message_source.source_platform_object
        components = await TelegramMessageConverter.yiri2target(message, self.bot)

        for component in components:
            if component['type'] != 'text':
                continue
            content = telegramify_markdown.markdownify(content=component['text']) if self.config.get('markdown_card', False) else component['text']
            args = {'chat_id': update.effective_chat.id, 'text': content}
            if self.config.get('markdown_card', False):
                args['parse_mode'] = 'MarkdownV2'
            if update.message and update.message.message_thread_id:
                args['message_thread_id'] = update.message.message_thread_id
            if quote_origin:
                args['reply_to_message_id'] = update.message.id
            await self.bot.send_message(**args)

    async def run_async(self) -> None:
        """LangBot 兼容。"""
        await self.start()

    async def kill(self) -> bool:
        """LangBot 兼容。"""
        await self.stop()
        return True
