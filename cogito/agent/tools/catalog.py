# cogito/agent/tools/catalog.py
#
# DefaultToolCatalog — toolset-aware visible tool selection.
#
# Design rules (see tool-system-spec §8):
#   - Resolves named toolsets from configuration (include/exclude/include_sets).
#   - Applies permission, risk, scope and model budget filters.
#   - Always-visible tools are always included.
#   - Recently-used tools are boosted via LRU tracking.
#   - Remaining tools are ranked by query relevance and capped by budget.

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Mapping

from cogito.agent.domain.tools import ToolDefinition, ToolKind, ToolRisk
from cogito.agent.ports.tools.catalog import (
    ToolCatalogPort,
    ToolSelectionRequest,
    VisibleToolSet,
)
from cogito.agent.tools.selector import HybridToolSelector
from cogito.agent.tools.registry import AtomicToolRegistry

logger = logging.getLogger(__name__)


class ToolsetConfig:
    """Configuration for one named toolset.

    Toolsets are resolved at composition-root time, not at each turn.
    Resolution includes cycle detection and missing reference checks.
    """

    def __init__(
        self,
        *,
        include: frozenset[str] = frozenset(),
        include_sets: frozenset[str] = frozenset(),
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        self.include = include
        self.include_sets = include_sets
        self.exclude = exclude


class DefaultToolCatalog:
    """Tool catalog with toolset resolution and selection delegation."""

    def __init__(
        self,
        *,
        registry: AtomicToolRegistry,
        selector: HybridToolSelector,
        toolsets: Mapping[str, ToolsetConfig] | None = None,
        always_visible: frozenset[str] | None = None,
    ) -> None:
        self._registry = registry
        self._selector = selector
        self._toolsets = dict(toolsets) if toolsets else {}
        self._always_visible = always_visible or frozenset()

        # Resolved toolset → tool names (cached at init)
        self._resolved_toolsets: dict[str, frozenset[str]] = {}
        self._resolve_all_toolsets()

    # ── Public API ──────────────────────────────────────────────────────

    async def select(
        self,
        request: ToolSelectionRequest,
    ) -> VisibleToolSet:
        """Select visible tools for one turn.

        The selection algorithm (tool-system-spec §8.4):
        1. Get all enabled, non-deprecated tools from registry.
        2. Apply toolset include/exclude.
        3. Apply actor/session permissions and scope limits.
        4. Apply risk and model capability filters.
        5. Add always-visible tools.
        6. Add recently-used tools (LRU).
        7. Search-rank remaining by query relevance.
        8. Cap by model_max_tools and budget.
        9. Mark over-cap tools as deferred.
        """
        snapshot = self._registry.snapshot()

        # Step 1-2: toolsets
        candidate_names = self._resolve_requested_toolsets(request)
        candidates = [
            snapshot.definitions[n]
            for n in candidate_names
            if n in snapshot.definitions
        ]

        # Step 3: enabled + non-deprecated
        candidates = [
            d for d in candidates
            if d.enabled and not d.deprecated
        ]

        # Step 4: risk filter
        allowed_risks = request.allowed_risks
        candidates = [
            d for d in candidates
            if d.risk in allowed_risks
        ]

        # Step 5: scope filter
        candidates = [
            d for d in candidates
            if request.scopes & d.scopes
        ]

        # Step 6: always-visible
        always_visible_defs = [
            snapshot.definitions[n]
            for n in self._always_visible
            if n in snapshot.definitions
        ]
        always_visible_names = frozenset(self._always_visible)

        # Remove always-visible from candidates to avoid double-count
        candidates = [d for d in candidates if d.name not in always_visible_names]

        # Deduplicate
        seen: set[str] = set()
        unique_candidates: list[ToolDefinition] = []
        for d in always_visible_defs + candidates:
            if d.name not in seen:
                seen.add(d.name)
                unique_candidates.append(d)

        # Step 7-8: delegate to selector
        selected, deferred, reasons = self._selector.select(
            candidates=unique_candidates,
            query=request.query,
            model_max_tools=request.model_max_tools,
            always_visible=self._always_visible,
        )

        selected_names = frozenset(d.name for d in selected)
        deferred_names = frozenset(d.name for d in deferred)

        return VisibleToolSet(
            registry_version=snapshot.version,
            definitions=tuple(selected),
            selected_names=selected_names,
            deferred_names=deferred_names,
            selection_reason=reasons,
        )

    # ── Toolset resolution ─────────────────────────────────────────────

    def _resolve_requested_toolsets(
        self,
        request: ToolSelectionRequest,
    ) -> frozenset[str]:
        """Resolve all requested toolsets into a flat set of tool names."""
        result: set[str] = set()

        for toolset_name in request.requested_toolsets:
            resolved = self._resolved_toolsets.get(toolset_name)
            if resolved is not None:
                result.update(resolved)

        return frozenset(result)

    def _resolve_all_toolsets(self) -> None:
        """Resolve all configured toolsets with cycle detection."""
        for name, config in self._toolsets.items():
            self._resolve_toolset(name, config, seen=set())

    def _resolve_toolset(
        self,
        name: str,
        config: ToolsetConfig,
        seen: set[str],
    ) -> frozenset[str]:
        """Resolve one toolset (recursive for include_sets)."""
        if name in self._resolved_toolsets:
            return self._resolved_toolsets[name]

        if name in seen:
            raise ValueError(f"Circular toolset dependency detected: {name}")
        seen.add(name)

        result: set[str] = set()

        # Direct includes
        result.update(config.include)

        # Recursive set includes
        for set_name in config.include_sets:
            child_config = self._toolsets.get(set_name)
            if child_config is None:
                raise ValueError(f"Toolset {name!r} references unknown set {set_name!r}")
            child_resolved = self._resolve_toolset(set_name, child_config, seen)
            result.update(child_resolved)

        # Excludes
        result.difference_update(config.exclude)

        resolved = frozenset(result)
        self._resolved_toolsets[name] = resolved
        return resolved
