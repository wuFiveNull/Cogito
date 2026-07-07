"""QQ Official event types — 从 LangBot 复制。

Source: langbot/libs/qq_official_api/qqofficialevent.py
"""
from __future__ import annotations

from typing import Any


class QQOfficialEvent(dict):
    """QQ 官方机器人事件。

    继承 dict，提供类型化属性访问。
    """

    @staticmethod
    def from_payload(payload: dict[str, Any]) -> QQOfficialEvent | None:
        try:
            return QQOfficialEvent(payload)
        except KeyError:
            return None

    @property
    def t(self) -> str:
        return str(self.get("t", ""))

    @property
    def user_openid(self) -> str:
        return str(self.get("user_openid", ""))

    @property
    def timestamp(self) -> str:
        return str(self.get("timestamp", ""))

    @property
    def d_author_id(self) -> str:
        return str(self.get("id", ""))

    @property
    def content(self) -> str:
        return str(self.get("content", ""))

    @property
    def d_id(self) -> str:
        return str(self.get("d_id", ""))

    @property
    def id(self) -> str:
        return str(self.get("id", ""))

    @property
    def channel_id(self) -> str:
        return str(self.get("channel_id", ""))

    @property
    def username(self) -> str:
        return str(self.get("username", ""))

    @property
    def guild_id(self) -> str:
        return str(self.get("guild_id", ""))

    @property
    def member_openid(self) -> str:
        return str(self.get("openid", ""))

    @property
    def attachments(self) -> str | None:
        raw = self.get("image_attachments")
        if not raw:
            return None
        url = str(raw)
        if not url.startswith("https://"):
            url = "https://" + url
        return url

    @property
    def group_openid(self) -> str:
        return str(self.get("group_openid", ""))

    @property
    def content_type(self) -> str:
        return str(self.get("content_type", ""))
