"""LangBot compatibility: Message types (MessageChain, Plain, Image, etc.)

Minimal stub that lets copied LangBot adapters run inside Cogito.
Replaces `langbot_plugin.api.entities.builtin.platform.message`.
"""
from __future__ import annotations

from typing import Any


class MessageComponent:
    """消息组件基类。"""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{self.__class__.__name__}({attrs})"


class Source(MessageComponent):
    """消息来源。"""

    def __init__(self, id: str = "", time: float = 0.0) -> None:  # noqa: A002
        super().__init__(id=id, time=time)


class Plain(MessageComponent):
    """纯文本组件。"""

    def __init__(self, text: str = "") -> None:
        super().__init__(text=text)


class At(MessageComponent):
    """@某人组件。"""

    def __init__(self, target: str = "", display: str = "") -> None:
        super().__init__(target=target, display=display)


class AtAll(MessageComponent):
    """@全体成员组件。"""

    def __init__(self) -> None:
        super().__init__()


class Image(MessageComponent):
    """图片组件。"""

    def __init__(
        self,
        base64: str | None = None,
        url: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(base64=base64, url=url, path=path)


class Voice(MessageComponent):
    """语音组件。"""

    def __init__(
        self,
        base64: str | None = None,
        url: str | None = None,
        path: str | None = None,
        length: int = 0,
    ) -> None:
        super().__init__(base64=base64, url=url, path=path, length=length)


class File(MessageComponent):
    """文件组件。"""

    def __init__(
        self,
        name: str = "",
        size: int = 0,
        base64: str | None = None,
        url: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(name=name, size=size, base64=base64, url=url, path=path)


class Forward(MessageComponent):
    """转发消息组件。"""

    def __init__(self, node_list: list | None = None) -> None:
        super().__init__(node_list=node_list or [])


class MessageChain(list):
    """消息链 —— 一系列 MessageComponent 的有序列表。

    用法:
        chain = MessageChain([Plain(text="Hello"), Image(base64="...")])
    """

    def __init__(self, components: list[MessageComponent] | None = None) -> None:
        super().__init__(components or [])

    def __repr__(self) -> str:
        return f"MessageChain({len(self)} components)"
