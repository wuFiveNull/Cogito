"""
cogito.channels.contract — Channel Protocol

Channel 只做两件事：
1. 将外部平台事件转换成标准入站消息提交到 InboundPort。
2. 将标准出站请求转换成平台 API 调用。

Channel 不直接操作 Session、AgentLoop、EventBus、ToolRegistry 或中断控制器。
"""

from __future__ import annotations

from typing import Protocol

from cogito.bus.events import DeliveryReceipt, OutboundRequest
from cogito.bus.inbound import InboundPort


class Channel(Protocol):
    """外部信道适配层协议。"""

    name: str

    async def run(self, inbound: InboundPort) -> None:
        """持续监听外部平台事件，并向 InboundPort 提交标准消息。"""

    async def send(
        self,
        request: OutboundRequest,
    ) -> DeliveryReceipt:
        """将标准出站请求发送到外部平台。"""

    async def close(self) -> None:
        """停止监听并释放连接、会话等资源。"""
