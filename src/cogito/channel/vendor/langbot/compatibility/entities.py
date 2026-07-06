"""LangBot compatibility: Entity types (Friend, Group, GroupMember, etc.)

Minimal stub that lets copied LangBot adapters run inside Cogito.
Replaces `langbot_plugin.api.entities.builtin.platform.entities`.
"""
from __future__ import annotations

from enum import IntEnum


class Permission(IntEnum):
    """群权限枚举。"""
    Member = 1
    Admin = 2
    Owner = 3


class Friend:
    """好友/联系人。"""

    def __init__(self, id: str = "", nickname: str = "", remark: str = "") -> None:  # noqa: A002
        self.id = id
        self.nickname = nickname
        self.remark = remark

    def __repr__(self) -> str:
        return f"Friend(id={self.id!r}, nickname={self.nickname!r})"


class Group:
    """群。"""

    def __init__(
        self,
        id: str = "",  # noqa: A002
        name: str = "",
        permission: Permission = Permission.Member,
    ) -> None:
        self.id = id
        self.name = name
        self.permission = permission

    def __repr__(self) -> str:
        return f"Group(id={self.id!r}, name={self.name!r})"


class GroupMember:
    """群成员。"""

    def __init__(
        self,
        id: str = "",  # noqa: A002
        member_name: str = "",
        permission: Permission = Permission.Member,
        group: Group | None = None,
        special_title: str = "",
    ) -> None:
        self.id = id
        self.member_name = member_name
        self.permission = permission
        self.group = group
        self.special_title = special_title

    def __repr__(self) -> str:
        return f"GroupMember(id={self.id!r}, name={self.member_name!r})"
