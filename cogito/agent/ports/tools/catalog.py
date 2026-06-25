# cogito/agent/ports/tools/catalog.py
#
# Tool Catalog Port — visible tool selection for each turn.
#
# Design rules (see tool-system-spec §8):
#   - Catalog queries the registry + toolset config + permissions.
#   - Select returns only what is visible for the current turn.
#   - Does NOT execute tools — only determines visibility.
#   - Tool search is a separate concern (builtin tool, not catalog method).

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from cogito.agent.domain.tools import ToolDefinition, ToolRisk


@dataclass(frozen=True, slots=True)
class ToolSelectionRequest:
    """Query for selecting visible tools for one turn."""
    actor_id: str
    session_id: str
    query: str
    requested_toolsets: tuple[str, ...]
    model_id: str
    model_max_tools: int
    registry_version: int
    allowed_risks: frozenset[ToolRisk]
    scopes: frozenset[str] = frozenset({"core"})


@dataclass(frozen=True, slots=True)
class VisibleToolSet:
    """The set of tools visible for one turn."""
    registry_version: int
    definitions: tuple[ToolDefinition, ...]
    selected_names: frozenset[str]
    deferred_names: frozenset[str]
    selection_reason: dict[str, str]


class ToolCatalogPort(Protocol):
    """Selects visible tools for a turn based on request and policy."""

    async def select(
        self,
        request: ToolSelectionRequest,
    ) -> VisibleToolSet:
        ...
