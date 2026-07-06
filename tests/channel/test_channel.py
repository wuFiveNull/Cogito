"""Tests for Channel integration.

覆盖：
- LangBot 兼容层类型
- Bridge LangBotEvent → Inbound 转换
- InboundDispatcher → ChannelEnvelope → InboundService
- ChannelManager 生命周期
- Telegram 适配器语法和协议验证
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.channel.base import AdapterStatus, ChannelAdapter
from cogito.channel.bridge import langbot_event_to_inbound
from cogito.inbound.dispatcher import InboundDispatcher
from cogito.inbound.models import Inbound, InboundContent, InboundRoute
from cogito.service.inbound_service import InboundService
from cogito.store.migration import migrate

# =============================================================================
# 兼容层测试
# =============================================================================


class TestCompatibilityLayer:
    def test_message_chain_basic(self):
        """创建 MessageChain 及其组件。"""
        from cogito.channel.vendor.langbot.compatibility.message import (
            File,
            Image,
            MessageChain,
            Plain,
        )

        chain = MessageChain([
            Plain(text="Hello"),
            Image(base64="abc123"),
            File(name="test.pdf", size=1024),
        ])
        assert len(chain) == 3
        assert isinstance(chain[0], Plain)
        assert isinstance(chain[1], Image)
        assert isinstance(chain[2], File)
        assert chain[0].text == "Hello"
        assert chain[1].base64 == "abc123"

    def test_message_chain_forward(self):
        """Forward 组件包含子消息链。"""
        from cogito.channel.vendor.langbot.compatibility.message import (
            Forward,
            MessageChain,
            Plain,
        )

        inner = MessageChain([Plain(text="nested")])
        fwd = Forward(node_list=[type("Node", (), {"message_chain": inner})()])
        assert len(fwd.node_list) == 1

    def test_friend_event(self):
        """FriendMessage 事件。"""
        from cogito.channel.vendor.langbot.compatibility.entities import Friend
        from cogito.channel.vendor.langbot.compatibility.events import FriendMessage
        from cogito.channel.vendor.langbot.compatibility.message import MessageChain, Plain

        friend = Friend(id="123", nickname="TestUser", remark="remark")
        chain = MessageChain([Plain(text="Hello")])
        event = FriendMessage(
            sender=friend,
            message_chain=chain,
            time=1000.0,
            source_platform_object=type("Obj", (), {"message": type("Msg", (), {"message_id": "m1"})()})(),
        )
        assert event.sender.id == "123"
        assert event.sender.nickname == "TestUser"

    def test_group_event(self):
        """GroupMessage 事件。"""
        from cogito.channel.vendor.langbot.compatibility.entities import (
            Group,
            GroupMember,
            Permission,
        )
        from cogito.channel.vendor.langbot.compatibility.events import GroupMessage
        from cogito.channel.vendor.langbot.compatibility.message import MessageChain, Plain

        group = Group(id="g1", name="Test Group", permission=Permission.Member)
        member = GroupMember(id="u1", member_name="User", permission=Permission.Member, group=group)
        chain = MessageChain([Plain(text="Hello group")])
        event = GroupMessage(
            sender=member,
            message_chain=chain,
            time=2000.0,
            source_platform_object=type("Obj", (), {"message": type("Msg", (), {"message_id": "m2"})()})(),
        )
        assert event.sender.group.id == "g1"
        assert event.sender.group.name == "Test Group"

    def test_event_logger(self):
        """EventLogger 可以记录消息。"""
        import asyncio

        from cogito.channel.vendor.langbot.compatibility.logger import EventLogger

        logger = EventLogger("test.channel")
        asyncio.run(logger.info("test info"))
        asyncio.run(logger.error("test error"))

    def test_adapter_status_enum(self):
        """AdapterStatus 枚举值正确。"""
        assert AdapterStatus.created == "created"
        assert AdapterStatus.running == "running"
        assert AdapterStatus.stopped == "stopped"


# =============================================================================
# Bridge 转换测试
# =============================================================================


class TestBridge:
    @pytest.mark.asyncio
    async def test_friend_message_to_inbound(self):
        """FriendMessage → Inbound 转换。"""
        from cogito.channel.vendor.langbot.compatibility.entities import Friend
        from cogito.channel.vendor.langbot.compatibility.events import FriendMessage
        from cogito.channel.vendor.langbot.compatibility.message import MessageChain, Plain

        friend = Friend(id="u123", nickname="User")
        chain = MessageChain([Plain(text="Hello Cogito!")])
        event = FriendMessage(
            sender=friend,
            message_chain=chain,
            time=3000.0,
            source_platform_object=type("Obj", (), {"message": type("Msg", (), {"message_id": "m_1"})()})(),
        )

        inbound = await langbot_event_to_inbound(event, "telegram", "tg_bot_1")

        assert inbound.channel == "telegram"
        assert inbound.channel_instance_id == "tg_bot_1"
        assert inbound.conversation_id == "u123"
        assert inbound.sender_id == "u123"
        assert len(inbound.content) == 1
        assert inbound.content[0].type == "text"
        assert inbound.content[0].data == "Hello Cogito!"
        assert inbound.route.channel_type == "telegram"

    @pytest.mark.asyncio
    async def test_group_message_uses_group_id(self):
        """GroupMessage 使用 group.id 作为 conversation_id。"""
        from cogito.channel.vendor.langbot.compatibility.entities import (
            Group,
            GroupMember,
            Permission,
        )
        from cogito.channel.vendor.langbot.compatibility.events import GroupMessage
        from cogito.channel.vendor.langbot.compatibility.message import MessageChain, Plain

        group = Group(id="g999", name="Group")
        member = GroupMember(id="u456", member_name="User", permission=Permission.Member, group=group)
        chain = MessageChain([Plain(text="Group msg")])
        event = GroupMessage(
            sender=member,
            message_chain=chain,
            time=4000.0,
            source_platform_object=type("Obj", (), {"message": type("Msg", (), {"message_id": "m_2"})()})(),
        )

        inbound = await langbot_event_to_inbound(event, "telegram", "tg_bot_1")

        assert inbound.conversation_id == "g999"
        assert inbound.sender_id == "u456"

    @pytest.mark.asyncio
    async def test_multi_content_extraction(self):
        """多种消息组件提取。"""
        from cogito.channel.vendor.langbot.compatibility.entities import Friend
        from cogito.channel.vendor.langbot.compatibility.events import FriendMessage
        from cogito.channel.vendor.langbot.compatibility.message import (
            File,
            Image,
            MessageChain,
            Plain,
        )

        friend = Friend(id="u1", nickname="User")
        chain = MessageChain([
            Plain(text="Text"),
            Image(base64="img_data"),
            File(name="doc.pdf", size=2048),
        ])
        event = FriendMessage(
            sender=friend,
            message_chain=chain,
            time=5000.0,
            source_platform_object=type("Obj", (), {"message": type("Msg", (), {"message_id": "m_3"})()})(),
        )

        inbound = await langbot_event_to_inbound(event, "test", "inst1")

        assert len(inbound.content) == 3
        assert inbound.content[0].type == "text"
        assert inbound.content[0].data == "Text"
        assert inbound.content[1].type == "image"
        assert inbound.content[1].data == "img_data"
        assert inbound.content[2].type == "file"
        assert inbound.content[2].name == "doc.pdf"

    @pytest.mark.asyncio
    async def test_empty_message_chain(self):
        """空消息链返回空 content。"""
        from cogito.channel.vendor.langbot.compatibility.entities import Friend
        from cogito.channel.vendor.langbot.compatibility.events import FriendMessage
        from cogito.channel.vendor.langbot.compatibility.message import MessageChain

        friend = Friend(id="u1", nickname="User")
        event = FriendMessage(
            sender=friend,
            message_chain=MessageChain([]),
            time=6000.0,
            source_platform_object=type("Obj", (), {"message": type("Msg", (), {"message_id": "m_4"})()})(),
        )

        inbound = await langbot_event_to_inbound(event, "test", "inst1")
        assert len(inbound.content) == 0


# =============================================================================
# InboundDispatcher 测试
# =============================================================================


class TestInboundDispatcher:
    def test_build_envelope_from_inbound(self, in_memory_db_service):
        """InboundDispatcher 将 Inbound 转换为 ChannelEnvelope 并调用 InboundService。"""
        dispatcher = InboundDispatcher(in_memory_db_service)

        inbound = Inbound(
            channel="telegram",
            channel_instance_id="tg_bot",
            conversation_id="conv_1",
            sender_id="user_1",
            message_id="pm_1",
            content=[InboundContent(type="text", data="Hello from Telegram!")],
            timestamp=int(datetime.now(UTC).timestamp()),
            route=InboundRoute(
                adapter_id="tg_bot",
                channel_type="telegram",
                conversation_id="conv_1",
                source_message_id="pm_1",
            ),
        )

        import asyncio
        asyncio.run(dispatcher.dispatch(inbound))

        # Verify the message was created via InboundService
        envelope = dispatcher._build_envelope(inbound)
        assert envelope.channel_type == "telegram"
        assert envelope.platform_sender_id == "user_1"
        assert envelope.platform_conversation_id == "conv_1"
        assert envelope.sender_endpoint_ref == "telegram:user_1"
        assert envelope.reply_route is not None

    def test_envelope_content_parts(self):
        """Inbound 的多段内容映射到 ChannelEnvelope content_parts。"""
        from cogito.inbound.dispatcher import InboundDispatcher

        inbound = Inbound(
            channel="test",
            channel_instance_id="inst1",
            conversation_id="c1",
            sender_id="s1",
            message_id="m1",
            content=[
                InboundContent(type="text", data="Hello"),
                InboundContent(type="image", data="img_base64", mime="image/png"),
            ],
            route=InboundRoute(adapter_id="a1", channel_type="test", conversation_id="c1", source_message_id="m1"),
        )

        # Access the private method for unit testing
        dispatcher = InboundDispatcher.__new__(InboundDispatcher)
        envelope = dispatcher._build_envelope(inbound)

        assert len(envelope.content_parts) == 2
        assert envelope.content_parts[0]["content_type"] == "text"
        assert envelope.content_parts[0]["inline_data"] == "Hello"
        assert envelope.content_parts[1]["content_type"] == "image"
        assert envelope.content_parts[1]["inline_data"] == "img_base64"
        assert envelope.content_parts[1]["mime"] == "image/png"


# =============================================================================
# ChannelManager 测试
# =============================================================================


class TestChannelManager:
    def test_registry_has_telegram(self):
        """registry 包含 telegram 适配器。"""
        from cogito.channel.registry import ADAPTERS

        assert "telegram" in ADAPTERS
        spec = ADAPTERS["telegram"]
        assert spec.class_name == "TelegramAdapter"
        assert "telegram" in spec.module

    def test_unknown_adapter_raises(self):
        """未知适配器名称抛出 ValueError。"""
        from cogito.channel.registry import create_adapter

        with pytest.raises(ValueError, match="Unknown channel adapter"):
            create_adapter("nonexistent", {})

    def test_channel_adapter_protocol(self):
        """ChannelAdapter Protocol 可被类实现。"""

        class FakeAdapter:
            def __init__(self):
                self.adapter_id = "fake"
                self.channel_type = "fake_type"
                self.status = AdapterStatus.created

            def set_inbound_handler(self, handler):
                pass

            async def start(self):
                self.status = AdapterStatus.running

            async def stop(self):
                self.status = AdapterStatus.stopped

            async def send(self, conversation_id, message, reply_to_message_id=None):
                return {"platform_message_id": "fake_id"}

        adapter = FakeAdapter()
        assert isinstance(adapter, ChannelAdapter)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def in_memory_db_service(in_memory_db: sqlite3.Connection) -> InboundService:
    return InboundService(in_memory_db)
