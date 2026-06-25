# cogito/agent/tools/providers.py
#
# ToolProvider implementations.
#
# BuiltinToolProvider — provides all built-in tools.
# ConfiguredToolProvider — loads tools from configuration.

from __future__ import annotations

import logging
from typing import Mapping

from cogito.agent.domain.tools import ToolDefinition
from cogito.agent.ports.tools.registry import ToolHandler, ToolProvider

logger = logging.getLogger(__name__)


class BuiltinToolProvider:
    """Provider for built-in tools.

    Built-in tools are always available and registered first, giving
    them the highest conflict-resolution priority.
    """

    def __init__(self, handlers: list[ToolHandler] | None = None) -> None:
        self._handlers = list(handlers) if handlers else []

    @property
    def name(self) -> str:
        return "builtin"

    async def load(self) -> list[ToolHandler]:
        """Return the built-in tool handlers."""
        return list(self._handlers)

    async def close(self) -> None:
        pass


class ConfiguredToolProvider:
    """Provider for tools loaded from configuration."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._handlers: list[ToolHandler] = []

    @property
    def name(self) -> str:
        return self._name

    def add_handler(self, handler: ToolHandler) -> None:
        self._handlers.append(handler)

    async def load(self) -> list[ToolHandler]:
        return list(self._handlers)

    async def close(self) -> None:
        pass
