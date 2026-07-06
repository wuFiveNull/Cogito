# cogito/agent/runtime/phase.py

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from cogito.agent.runtime.context import TurnContext


class RuntimePhase(Protocol):
    """Protocol that all runtime phases must satisfy."""

    @property
    def name(self) -> str:
        ...

    async def run(self, ctx: TurnContext) -> None:
        ...


class BasePhase(ABC):
    """Abstract base class for phases with a fixed name."""

    name: str

    async def run(self, ctx: TurnContext) -> None:
        await self.execute(ctx)

    @abstractmethod
    async def execute(self, ctx: TurnContext) -> None:
        ...
