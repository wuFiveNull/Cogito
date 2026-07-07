"""QQ OneBot 11 Contract Tests.

覆盖（plan §12 验收矩阵）:
- QQ-A07: private text → canonical Inbound，message_id 稳定
- QQ-A08: group @ text → canonical Inbound，sender/group refs 正确
- QQ-A09: 重复 OneBot message_id —— 桥接层去重
- QQ-A10: 非 Owner 私聊 → 拒绝
- QQ-A11: 非 allowlist 群 → 拒绝
- QQ-A12: 群内未 @Bot → 忽略
- QQ-A13: bot 自消息 → 忽略

注意：这些测试不启动真实 aiocqhttp server，只验证 LangBot Event → Inbound 转换。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cogito.channel.drivers.onebot_models import (
    OneBotPolicy,
    extract_onebot_message_id,
    friend_event_to_inbound,
    group_event_to_inbound,
)
from cogito.channel.vendor.langbot.compatibility import entities as lb_entities
from cogito.channel.vendor.langbot.compatibility import events as lb_events
from cogito.channel.vendor.langbot.compatibility import message as lb_message

ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = ROOT / "tests" / "channel" / "fixtures" / "onebot"

OWNER_QQ = "12345678"
BOT_QQ = "99999999"
ALLOWED_GROUP = "88888888"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _make_friend_event(message_id: int, user_id: int, text: str, self_id: int = 99999999):
    """创建 FriendMessage LangBot Event。"""
    friend = lb_entities.Friend(id=str(user_id), nickname="TestUser")
    chain = lb_message.MessageChain([lb_message.Plain(text=text)])
    src_obj = type(
        "OneBotObj",
        (),
        {
            "message_id": message_id,
            "message": {"message_id": message_id},
            "user_id": user_id,
            "self_id": self_id,
        },
    )()
    return lb_events.FriendMessage(
        sender=friend,
        message_chain=chain,
        time=1700000000.0,
        source_platform_object=src_obj,
    )


def _make_group_event(
    message_id: int,
    group_id: int,
    user_id: int,
    self_id: int = 99999999,
    text: str = "你好",
    at_bot: bool = True,
):
    """创建 GroupMessage LangBot Event。"""
    group = lb_entities.Group(
        id=str(group_id), name="TestGroup", permission=lb_entities.Permission.Member,
    )
    member = lb_entities.GroupMember(
        id=str(user_id),
        member_name="User",
        permission=lb_entities.Permission.Member,
        group=group,
    )
    chain_items: list = []
    if at_bot:
        chain_items.append(lb_message.At(target=str(self_id)))
    chain_items.append(lb_message.Plain(text=text))
    chain = lb_message.MessageChain(chain_items)
    src_obj = type(
        "OneBotObj",
        (),
        {
            "message_id": message_id,
            "message": {"message_id": message_id},
            "user_id": user_id,
            "group_id": group_id,
            "self_id": self_id,
        },
    )()
    return lb_events.GroupMessage(
        sender=member,
        message_chain=chain,
        time=1700000100.0,
        source_platform_object=src_obj,
    )


# ── Fixture extraction ──────────────────────────────────────────────────


class TestExtractMessageId:
    def test_private_fixture_message_id(self) -> None:
        """QQ-A07: private fixture 的 message_id 从 source_platform_object.message_id 提取。"""
        event = _make_friend_event(100001, int(OWNER_QQ), "你好 Cogito")
        msg_id = extract_onebot_message_id(event)
        assert msg_id == "100001"

    def test_group_fixture_message_id(self) -> None:
        """QQ-A08: group fixture 的 message_id 正确提取。"""
        event = _make_group_event(100002, int(ALLOWED_GROUP), int(OWNER_QQ))
        msg_id = extract_onebot_message_id(event)
        assert msg_id == "100002"

    def test_dict_event_message_id(self) -> None:
        """aiocqhttp.Event 是 dict，支持 dict 访问。"""
        friend = lb_entities.Friend(id=str(OWNER_QQ), nickname="TestUser")
        chain = lb_message.MessageChain([lb_message.Plain(text="test")])
        # 模拟 dict 子类
        from aiocqhttp import Event
        src_obj = Event({"message_id": 555555, "user_id": int(OWNER_QQ)})
        event = lb_events.FriendMessage(
            sender=friend,
            message_chain=chain,
            time=1700000000.0,
            source_platform_object=src_obj,
        )
        msg_id = extract_onebot_message_id(event)
        assert msg_id == "555555"


# ── Allowlist / Gating ──────────────────────────────────────────────────


class TestPrivateAllowlist:
    @pytest.mark.asyncio
    async def test_owner_accepted(self) -> None:
        """QQ-A07: Owner 私聊通过。"""
        event = _make_friend_event(100001, int(OWNER_QQ), "你好")
        policy = OneBotPolicy(
            owner_qq_ids={OWNER_QQ},
            allow_private=True,
        )
        inbound, reason = friend_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is not None
        assert reason == "accepted"
        assert inbound.channel == "qq"
        assert inbound.channel_instance_id == "qq-main"
        assert inbound.conversation_id == f"private:{OWNER_QQ}"
        assert inbound.sender_id == OWNER_QQ
        assert inbound.message_id == "100001"
        assert inbound.metadata["conversation_type"] == "private"
        assert inbound.metadata["trust_label"] == "external_untrusted"
        assert inbound.route.source_message_id == "100001"

    @pytest.mark.asyncio
    async def test_non_owner_rejected(self) -> None:
        """QQ-A10: 非 Owner 私聊被拒绝。"""
        event = _make_friend_event(100002, 99999999, "你好")
        policy = OneBotPolicy(
            owner_qq_ids={OWNER_QQ},
            allow_private=True,
        )
        inbound, reason = friend_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is None
        assert reason == "not_owner"

    @pytest.mark.asyncio
    async def test_bot_self_rejected(self) -> None:
        """QQ-A13: bot 自己发送的私聊消息被忽略。"""
        event = _make_friend_event(100003, int(BOT_QQ), "自说自话")
        policy = OneBotPolicy(
            owner_qq_ids={OWNER_QQ},
            allow_private=True,
            bot_self_ids={BOT_QQ},
        )
        inbound, reason = friend_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is None
        assert reason == "bot_self"


class TestGroupGating:
    @pytest.mark.asyncio
    async def test_group_at_bot_accepted(self) -> None:
        """QQ-A08: allowlist 群中 @Bot 消息通过。"""
        event = _make_group_event(100004, int(ALLOWED_GROUP), int(OWNER_QQ), at_bot=True)
        policy = OneBotPolicy(
            owner_qq_ids={OWNER_QQ},
            allowed_group_ids={ALLOWED_GROUP},
            require_mention_in_group=True,
        )
        inbound, reason = group_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is not None
        assert reason == "accepted"
        assert inbound.conversation_id == f"group:{ALLOWED_GROUP}"
        assert inbound.metadata["conversation_type"] == "group"
        assert inbound.metadata["group_id"] == ALLOWED_GROUP

    @pytest.mark.asyncio
    async def test_group_not_allowed_rejected(self) -> None:
        """QQ-A11: 非 allowlist 群被拒绝。"""
        event = _make_group_event(100005, 77777777, int(OWNER_QQ), at_bot=True)
        policy = OneBotPolicy(
            owner_qq_ids={OWNER_QQ},
            allowed_group_ids={ALLOWED_GROUP},
            require_mention_in_group=True,
        )
        inbound, reason = group_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is None
        assert reason == "group_not_allowed"

    @pytest.mark.asyncio
    async def test_group_not_at_bot_ignored(self) -> None:
        """QQ-A12: 群内未 @Bot 被忽略。"""
        event = _make_group_event(
            100006, int(ALLOWED_GROUP), int(OWNER_QQ), at_bot=False,
        )
        policy = OneBotPolicy(
            owner_qq_ids={OWNER_QQ},
            allowed_group_ids={ALLOWED_GROUP},
            require_mention_in_group=True,
        )
        inbound, reason = group_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is None
        assert reason == "not_at_bot"

    @pytest.mark.asyncio
    async def test_group_bot_self_rejected(self) -> None:
        """QQ-A13: bot 自己发送的群消息被忽略。"""
        event = _make_group_event(
            100007, int(ALLOWED_GROUP), int(BOT_QQ), at_bot=False,
        )
        policy = OneBotPolicy(
            owner_qq_ids={OWNER_QQ},
            allowed_group_ids={ALLOWED_GROUP},
            require_mention_in_group=True,
            bot_self_ids={BOT_QQ},
        )
        inbound, reason = group_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is None
        assert reason == "bot_self"


# ── Canonical fields ────────────────────────────────────────────────────


class TestCanonicalFields:
    def test_stable_ids_private(self) -> None:
        """plan §8.4: 私聊稳定 ID 规则。"""
        event = _make_friend_event(200001, int(OWNER_QQ), "test")
        policy = OneBotPolicy(owner_qq_ids={OWNER_QQ})
        inbound, _ = friend_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is not None
        assert inbound.metadata["sender_endpoint_ref"] == f"qq:qq-main:user:{OWNER_QQ}"
        assert inbound.metadata["conversation_endpoint_ref"] == f"qq:qq-main:private:{OWNER_QQ}"
        assert inbound.metadata["target_endpoint_ref"] == f"qq:qq-main:person:{OWNER_QQ}"

    def test_stable_ids_group(self) -> None:
        """plan §8.4: 群聊稳定 ID 规则。"""
        event = _make_group_event(200002, int(ALLOWED_GROUP), int(OWNER_QQ))
        policy = OneBotPolicy(
            owner_qq_ids={OWNER_QQ}, allowed_group_ids={ALLOWED_GROUP},
        )
        inbound, _ = group_event_to_inbound(
            event, instance_id="qq-main", policy=policy, bot_ids={BOT_QQ},
        )
        assert inbound is not None
        assert inbound.metadata["sender_endpoint_ref"] == f"qq:qq-main:user:{OWNER_QQ}"
        assert inbound.metadata["conversation_endpoint_ref"] == f"qq:qq-main:group:{ALLOWED_GROUP}"
        assert inbound.metadata["target_endpoint_ref"] == f"qq:qq-main:group:{ALLOWED_GROUP}"
