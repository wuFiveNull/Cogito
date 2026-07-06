"""LangBot compatibility: EventLogger.

Minimal stub that lets copied LangBot adapters run inside Cogito.
Replaces `langbot_plugin.api.definition.abstract.platform.event_logger`
and `langbot.pkg.platform.logger`.
"""
from __future__ import annotations

import logging

from .adapter import AbstractEventLogger


class EventLogger(AbstractEventLogger):
    """事件日志记录器 —— 基于标准库 logging。

    LangBot 的 EventLogger 接受 channel 名称作为参数:
        logger = EventLogger("channel.telegram")
    """

    def __init__(self, name: str = "cogito.channel") -> None:
        self._logger = logging.getLogger(name)

    async def info(self, message: str) -> None:
        self._logger.info(message)

    async def error(self, message: str) -> None:
        self._logger.error(message)

    async def warning(self, message: str) -> None:
        self._logger.warning(message)

    async def debug(self, message: str) -> None:
        self._logger.debug(message)
