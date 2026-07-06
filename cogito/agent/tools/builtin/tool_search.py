# cogito/agent/tools/builtin/tool_search.py
#
# Built-in tool: tool_search — searches available tools by name/description.
#
# This is a meta-tool: it lets the model discover tools it might not
# have been shown due to visibility limits.

from __future__ import annotations

import json
from typing import Mapping

from cogito.agent.domain.tools import (
    ToolConcurrencyMode,
    ToolDefinition,
    ToolKind,
    ToolLimits,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)
from cogito.agent.ports.tools.registry import ToolHandler, ToolRegistrySnapshot


class ToolSearchHandler:
    """Handler for the tool_search built-in tool.

    Searches the registry for tools matching a query and returns
    matching tool definitions as a JSON result.
    """

    def __init__(self, registry: object) -> None:
        self._registry = registry

    def _snapshot(self):
        """Get current registry snapshot."""
        if hasattr(self._registry, "snapshot"):
            return self._registry.snapshot()
        return None

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="tool_search",
            description="Search for available tools by name, description, or tags. Returns matching tool names and descriptions.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Search query to match against tool names and descriptions",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum number of results to return (default 10)",
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional filter by tool kind (read, search, edit, etc.)",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=5.0,
            idempotent=True,
            parallel_safe=True,
            kind=ToolKind.SEARCH,
            risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
            limits=ToolLimits(timeout_seconds=5.0, max_result_chars=10_000),
            always_visible=True,
        )

    async def execute(
        self,
        *,
        arguments: Mapping[str, object],
        context: Mapping[str, object],
    ) -> dict[str, object]:
        """Execute tool_search."""
        query = str(arguments.get("query", ""))
        limit = int(arguments.get("limit", 10))
        kinds_filter = arguments.get("kinds", None)

        results = []
        snap = self._snapshot()
        if snap is None:
            return {"results": [], "total": 0, "query": query}

        for name, defn in snap.definitions.items():
            if len(results) >= limit:
                break

            # Query matching
            query_lower = query.lower()
            name_match = query_lower in name.lower()
            desc_match = query_lower in defn.description.lower()
            tag_match = any(query_lower in t.lower() for t in defn.tags)

            # Kind filter
            kind_match = True
            if kinds_filter and isinstance(kinds_filter, list):
                kind_match = defn.kind.value in [str(k).lower() for k in kinds_filter]

            if (name_match or desc_match or tag_match) and kind_match:
                results.append({
                    "name": name,
                    "description": defn.description[:200],
                    "kind": defn.kind.value,
                    "risk": defn.risk.value,
                    "deprecated": defn.deprecated,
                })

        return {
            "results": results,
            "total": len(results),
            "query": query,
        }
