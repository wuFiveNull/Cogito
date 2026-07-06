# cogito/agent/tools/registry.py
#
# AtomicToolRegistry — versioned, atomic tool handler manager.
#
# Design rules (see tool-system-spec §7):
#   - Registry is the single source of truth for tool definitions + handlers.
#   - Snapshots are immutable and versioned.
#   - Provider-level atomic replace prevents partial updates.
#   - Conflict policy is set at composition root, not per-registration.
#   - Registry does NOT handle user permissions, approval, or execution.

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Mapping

from cogito.agent.domain.tools import ToolDefinition, ToolSource, ToolSourceType
from cogito.agent.ports.tools.registry import (
    ToolConflictPolicy,
    ToolHandler,
    ToolRegistryPort,
    ToolRegistrySnapshot,
)

logger = logging.getLogger(__name__)

# Builtin provider priority — always wins in conflict
_BUILTIN_PROVIDER = "builtin"


class AtomicToolRegistry:
    """Thread-safe, versioned registry of tool handlers.

    Internal state is protected by a lock.  All mutations happen
    atomically: the new dict and handler map are fully constructed
    before the public snapshot is swapped.
    """

    def __init__(
        self,
        *,
        conflict_policy: ToolConflictPolicy = ToolConflictPolicy.ERROR,
        initial_handlers: list[ToolHandler] | None = None,
    ) -> None:
        self._conflict_policy = conflict_policy
        self._lock = _create_lock()

        # Provider-name → handler list (for atomic replacement)
        self._provider_handlers: dict[str, list[ToolHandler]] = {}

        # Built indices
        self._version = 0
        self._definitions: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._snapshot_created_at = datetime.now(timezone.utc)

        if initial_handlers:
            self._add_many(initial_handlers)

    # ── Public API ──────────────────────────────────────────────────────

    def snapshot(self) -> ToolRegistrySnapshot:
        """Return the current immutable snapshot."""
        return ToolRegistrySnapshot(
            version=self._version,
            definitions=dict(self._definitions),
            handlers=dict(self._handlers),
            created_at=self._snapshot_created_at,
        )

    def resolve(
        self,
        name: str,
        *,
        version: int | None = None,
    ) -> ToolHandler:
        """Resolve a handler by name at the current (or specified) version."""
        if version is not None and version != self._version:
            raise ValueError(
                f"Registry version {version} is not current ({self._version})",
            )
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"Tool not found: {name!r}")
        return handler

    def resolve_def(
        self,
        name: str,
    ) -> ToolDefinition | None:
        """Resolve a definition by name."""
        return self._definitions.get(name)

    async def replace_provider_tools(
        self,
        *,
        provider_name: str,
        handlers: list[ToolHandler],
    ) -> ToolRegistrySnapshot:
        """Atomically replace all tools for one provider.

        This is the primary mutation path.  It validates definitions
        and resolves conflicts before publishing a new snapshot.
        """
        return self._replace_provider(provider_name, handlers)

    # ── Legacy compat: resolve by name from an available_tools list ──────

    def resolve_from_list(
        self,
        *,
        name: str,
        available_tools: tuple[ToolDefinition, ...],
    ) -> ToolDefinition | None:
        """Resolve a tool name against an explicit list (legacy AgentLoop)."""
        for td in available_tools:
            if td.name == name:
                return td
        return None

    def validate_arguments(
        self,
        *,
        definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> None:
        """Legacy stub: argument validation is handled by JsonSchemaToolValidator."""
        pass

    # ── Internal ─────────────────────────────────────────────────────────

    def _replace_provider(
        self,
        provider_name: str,
        handlers: list[ToolHandler],
    ) -> ToolRegistrySnapshot:
        """Atomic provider-level replacement under lock."""
        with self._lock:
            # Validate all definitions first
            for h in handlers:
                self._validate_definition(h.definition, provider_name)

            # Build new state
            new_provider_handlers = dict(self._provider_handlers)
            new_provider_handlers[provider_name] = list(handlers)

            new_defs: dict[str, ToolDefinition] = {}
            new_handlers: dict[str, ToolHandler] = {}

            for pname, phandlers in new_provider_handlers.items():
                for h in phandlers:
                    name = h.definition.name
                    existing = new_defs.get(name)

                    if existing is not None and existing != h.definition:
                        # Conflict — apply policy
                        resolved = self._resolve_conflict(
                            existing_name=name,
                            existing_provider=self._provider_for_name(name, self._provider_handlers),
                            incoming_provider=provider_name,
                        )
                        if resolved is None:
                            # Skip this tool (keep existing)
                            continue
                        elif resolved == "replace":
                            # Overwrite
                            pass
                        elif resolved == "rename":
                            name = self._rename_tool(name, pname)

                    new_defs[name] = h.definition
                    new_handlers[name] = h

            # Replace global state
            self._provider_handlers = new_provider_handlers
            self._version += 1
            self._definitions = new_defs
            self._handlers = new_handlers
            self._snapshot_created_at = datetime.now(timezone.utc)

            return self.snapshot()

    def _validate_definition(
        self,
        definition: ToolDefinition,
        provider_name: str,
    ) -> None:
        """Validate a single tool definition before registration."""
        import re

        # Name must match ^[a-z][a-z0-9_]{0,63}$
        if not re.match(r"^[a-z][a-z0-9_]{0,63}$", definition.name):
            raise ValueError(
                f"Tool name {definition.name!r} from provider {provider_name!r} "
                f"must match ^[a-z][a-z0-9_]{0,63}$",
            )

        # Description length
        if len(definition.description) > 2_000:
            raise ValueError(
                f"Tool {definition.name!r} description exceeds 2000 chars",
            )

        # Schema must be a JSON Schema object
        schema = definition.input_schema
        if not isinstance(schema, dict):
            raise TypeError(
                f"Tool {definition.name!r} input_schema must be a dict",
            )
        if schema.get("type") != "object":
            raise ValueError(
                f"Tool {definition.name!r} input_schema root type must be 'object'",
            )

    def _resolve_conflict(
        self,
        *,
        existing_name: str,
        existing_provider: str | None,
        incoming_provider: str,
    ) -> str | None:
        """Resolve a name conflict per policy. Returns None=skip, 'replace'=overwrite, 'rename'=rename_source."""
        if self._conflict_policy is ToolConflictPolicy.ERROR:
            raise ValueError(
                f"Tool name conflict: {existing_name!r} already registered "
                f"by provider {existing_provider!r}, "
                f"cannot register from {incoming_provider!r}",
            )

        if self._conflict_policy is ToolConflictPolicy.KEEP_EXISTING:
            return None  # skip

        if self._conflict_policy is ToolConflictPolicy.REPLACE:
            # Builtin always wins, otherwise the latest provider wins
            if existing_provider == _BUILTIN_PROVIDER:
                return None  # builtin is authoritative
            return "replace"

        if self._conflict_policy is ToolConflictPolicy.RENAME_SOURCE:
            return "rename"

        return None

    @staticmethod
    def _rename_tool(name: str, provider: str) -> str:
        """Rename a tool to avoid conflict: {provider}_{name}."""
        return f"{provider}_{name}"

    @staticmethod
    def _provider_for_name(
        name: str,
        provider_handlers: dict[str, list[ToolHandler]],
    ) -> str | None:
        for pname, handlers in provider_handlers.items():
            for h in handlers:
                if h.definition.name == name:
                    return pname
        return None

    def _add_many(self, handlers: list[ToolHandler]) -> None:
        """Add initial handlers (bootstrap-time only, no lock)."""
        for h in handlers:
            self._definitions[h.definition.name] = h.definition
            self._handlers[h.definition.name] = h
        self._version = 1


def _create_lock():
    """Create a lock appropriate for the event loop."""
    import threading
    return threading.Lock()
