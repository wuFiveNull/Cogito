"""LangBot compatibility: Abstract adapter base classes.

Minimal stub that lets copied LangBot adapters run inside Cogito.
Replaces `langbot_plugin.api.definition.abstract.platform.adapter`
and `langbot_plugin.api.definition.abstract.platform.event_logger`.
"""
from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from typing import Any

import pydantic

from .events import Event, MessageEvent
from .message import MessageChain


class AbstractMessageConverter(ABC):
    """消息转换器 —— 在 MessageChain 和平台格式之间转换。"""

    @staticmethod
    async def yiri2target(message_chain: MessageChain, *args, **kwargs) -> list[dict]:
        raise NotImplementedError

    @staticmethod
    async def target2yiri(*args, **kwargs) -> MessageChain | Any:
        raise NotImplementedError


class AbstractEventConverter(ABC):
    """事件转换器 —— 在平台事件和 LangBot 事件之间转换。"""

    @staticmethod
    async def yiri2target(event: MessageEvent, *args, **kwargs) -> Any:
        raise NotImplementedError

    @staticmethod
    async def target2yiri(*args, **kwargs) -> MessageEvent | Any:
        raise NotImplementedError


class AbstractMessagePlatformAdapter(pydantic.BaseModel, ABC):
    """消息平台适配器基类。

    pydantic.BaseModel 允许子类声明 bot、application 等复杂字段。
    """

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)

    message_converter: AbstractMessageConverter | None = None
    event_converter: AbstractEventConverter | None = None
    config: dict = {}
    msg_stream_id: dict = {}
    seq: int = 1
    listeners: dict[
        type[Event],
        typing.Callable[[Event, AbstractMessagePlatformAdapter], None],
    ] = {}
    bot_account_id: str = ""

    @abstractmethod
    async def send_message(
        self, target_type: str, target_id: str, message: MessageChain,
    ) -> Any:
        ...

    @abstractmethod
    async def reply_message(
        self,
        message_source: MessageEvent,
        message: MessageChain,
        quote_origin: bool = False,
    ) -> Any:
        ...

    @abstractmethod
    async def run_async(self) -> None:
        ...

    @abstractmethod
    async def kill(self) -> bool:
        ...

    @abstractmethod
    async def is_stream_output_supported(self) -> bool:
        ...

    def register_listener(
        self,
        event_type: type[Event],
        callback: typing.Callable[
            [Event, AbstractMessagePlatformAdapter], None
        ],
    ) -> None:
        self.listeners[event_type] = callback

    def unregister_listener(
        self,
        event_type: type[Event],
        callback: typing.Callable[
            [Event, AbstractMessagePlatformAdapter], None
        ],
    ) -> None:
        self.listeners.pop(event_type, None)


class AbstractEventLogger(ABC):
    """事件日志记录器。"""

    @abstractmethod
    async def info(self, message: str) -> None:
        ...

    @abstractmethod
    async def error(self, message: str) -> None:
        ...

    @abstractmethod
    async def warning(self, message: str) -> None:
        ...

    @abstractmethod
    async def debug(self, message: str) -> None:
        ...
