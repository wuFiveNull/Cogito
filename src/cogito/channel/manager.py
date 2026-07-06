"""Channel Manager — 适配器生命周期管理。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from cogito.channel.base import ChannelAdapter
from cogito.channel.registry import create_adapter
from cogito.inbound.dispatcher import InboundDispatcher


class ChannelManager:
    """Channel 适配器管理器。

    负责：
    - 按配置创建/注册适配器
    - 启动/停止适配器
    - 将适配器的入站消息通过 InboundDispatcher 交给 Core
    """

    def __init__(self, dispatcher: InboundDispatcher) -> None:
        self._dispatcher = dispatcher
        self._adapters: dict[str, ChannelAdapter] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._log = logging.getLogger("cogito.channel.manager")

    async def start_channel(
        self, name: str, config: dict[str, Any],
    ) -> ChannelAdapter:
        """创建并启动一个 Channel 适配器。"""
        if name in self._adapters:
            raise RuntimeError(f"Channel {name!r} is already running")

        adapter = create_adapter(name, config)

        # 设置入站处理器 —— adapter 收到消息后调用此 handler
        async def handle_inbound(inbound):
            await self._dispatcher.dispatch(inbound)

        adapter.set_inbound_handler(handle_inbound)

        # 启动
        task = asyncio.create_task(
            self._run_adapter(name, adapter),
            name=f"channel:{name}",
        )
        self._adapters[name] = adapter
        self._tasks[name] = task
        self._log.info("Started channel %s", name)
        return adapter

    async def stop_channel(self, name: str) -> None:
        """停止一个 Channel 适配器。"""
        adapter = self._adapters.get(name)
        if adapter is None:
            return
        try:
            await adapter.stop()
        except Exception:
            self._log.exception("Error stopping channel %s", name)
        task = self._tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
        self._adapters.pop(name, None)
        self._log.info("Stopped channel %s", name)

    async def stop_all(self) -> None:
        """停止所有适配器。"""
        for name in list(self._adapters):
            await self.stop_channel(name)

    async def _run_adapter(self, name: str, adapter: ChannelAdapter) -> None:
        """运行适配器的后台任务。"""
        try:
            await adapter.start()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._log.exception("Channel %s failed", name)
