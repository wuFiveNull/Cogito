"""TurnService protocol."""

from __future__ import annotations

from typing import Any, Protocol

from cogito.contracts.envelope import ChannelEnvelope


class TurnAccepted:
    """Result of accepting a turn."""

    def __init__(self, turn_id: str, attempt_id: str, message_id: str) -> None:
        self.turn_id = turn_id
        self.attempt_id = attempt_id
        self.message_id = message_id


class ResumeCommand:
    """Command to resume a waiting turn."""

    def __init__(self, turn_id: str, resolution: str, payload: dict[str, Any] | None = None) -> None:
        self.turn_id = turn_id
        self.resolution = resolution
        self.payload = payload or {}


class TurnService(Protocol):
    """Turn 生命周期管理接口。"""

    async def accept(self, envelope: ChannelEnvelope) -> TurnAccepted:
        """接受 Channel 入站消息，创建 Turn + RunAttempt。"""
        ...

    async def cancel(self, turn_id: str, reason: str) -> None:
        """取消正在运行的 Turn。"""
        ...

    async def resume(self, turn_id: str, command: ResumeCommand) -> None:
        """恢复 waiting 状态的 Turn。"""
        ...
