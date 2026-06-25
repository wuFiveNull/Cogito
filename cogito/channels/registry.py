"""
cogito.channels.registry — ChannelRegistry

所有 Channel 统一注册到 ChannelRegistry，不再使用
bus.subscribe_outbound(channel, callback) 或 push_tool.register_channel(channel, sender)。
"""

from __future__ import annotations

from cogito.channels.contract import Channel


class UnknownChannelError(KeyError):
    """请求的 Channel 未注册。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown channel: {name!r}")
        self.channel_name = name


class ChannelRegistry:
    """信道注册中心。"""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}

    def register(self, channel: Channel) -> None:
        if channel.name in self._channels:
            raise ValueError(
                f"Channel already registered: {channel.name!r}",
            )
        self._channels[channel.name] = channel

    def get(self, name: str) -> Channel:
        try:
            return self._channels[name]
        except KeyError as exc:
            raise UnknownChannelError(name) from exc

    def all(self) -> tuple[Channel, ...]:
        return tuple(self._channels.values())
