# cogito/agent/ports/tools/registry.py
#
# Tool Registry Ports — versioned, atomic tool handler management.
#
# Design rules (see tool-system-spec §7):
#   - Registry is the single source of truth for tool definitions + handlers.
#   - Snapshots are immutable and versioned — consumers hold a version reference.
#   - Provider-level atomic replace prevents partial updates.
#   - Handler is the execution entry point; Provider discovers handlers.
#   - Conflict policy is set at composition root, not per-registration.

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping, Protocol

from cogito.agent.domain.tools import ToolDefinition


# ═════════════════════════════════════════════════════════════════════════
# Handler protocol
# ═════════════════════════════════════════════════════════════════════════


class ToolHandler(Protocol):
    """Executes a single tool and returns a result.

    Every tool in the registry has one handler.  The handler is the
    concrete implementation — it may call a Python function, a subprocess,
    an MCP server, or a remote API.

    ``definition`` provides the stable metadata.  ``execute`` runs the
    tool with validated arguments and a serialisable execution context.
    """

    @property
    def definition(self) -> ToolDefinition:
        ...

    async def execute(
        self,
        *,
        arguments: Mapping[str, object],
        context: Mapping[str, object],
    ) -> Mapping[str, object]:
        ...


class StreamingToolHandler(ToolHandler, Protocol):
    """Optional streaming variant of ToolHandler.

    The orchestrator checks if a handler implements this protocol;
    if so, it can stream progress events before the final result.
    """

    async def stream_execute(
        self,
        *,
        arguments: Mapping[str, object],
        context: Mapping[str, object],
    ) -> AsyncIterator[Mapping[str, object]]:
        ...


# ═════════════════════════════════════════════════════════════════════════
# Provider protocol
# ═════════════════════════════════════════════════════════════════════════


class ToolProvider(Protocol):
    """Discovers or constructs a set of tool handlers.

    ``load`` returns all handlers this provider knows about.
    ``close`` cleanly shuts down connections (MCP, remote, …).
    """

    @property
    def name(self) -> str:
        ...

    async def load(self) -> list[ToolHandler]:
        ...

    async def close(self) -> None:
        ...


# ═════════════════════════════════════════════════════════════════════════
# Snapshot & conflict policy
# ═════════════════════════════════════════════════════════════════════════


class ToolConflictPolicy(StrEnum):
    ERROR = "error"
    KEEP_EXISTING = "keep_existing"
    REPLACE = "replace"
    RENAME_SOURCE = "rename_source"


@dataclass(frozen=True, slots=True)
class ToolRegistrySnapshot:
    """Immutable point-in-time view of the registry."""

    version: int
    definitions: Mapping[str, ToolDefinition]
    handlers: Mapping[str, ToolHandler]
    created_at: datetime


# ═════════════════════════════════════════════════════════════════════════
# Registry port
# ═════════════════════════════════════════════════════════════════════════


class ToolRegistryPort(Protocol):
    """Versioned, atomic registry of tool handlers.

    Methods:
        snapshot:     Current immutable snapshot.  AgentLoopPhase binds
                      to a snapshot version for the duration of one turn.
        resolve:      Look up a handler by name at a specific version.
        resolve_def:  Look up a definition by name.
        replace_provider_tools: Atomically replace all tools for one
                      provider.  Returns a new snapshot.
    """

    def snapshot(self) -> ToolRegistrySnapshot:
        ...

    def resolve(
        self,
        name: str,
        *,
        version: int | None = None,
    ) -> ToolHandler:
        ...

    def resolve_def(
        self,
        name: str,
    ) -> ToolDefinition | None:
        ...

    async def replace_provider_tools(
        self,
        *,
        provider_name: str,
        handlers: list[ToolHandler],
    ) -> ToolRegistrySnapshot:
        ...
