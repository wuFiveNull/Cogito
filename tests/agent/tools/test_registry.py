"""Tests for AtomicToolRegistry — versioned, atomic tool handler management."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

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
from cogito.agent.ports.tools.registry import (
    ToolConflictPolicy,
    ToolHandler,
    ToolRegistryPort,
)
from cogito.agent.tools.registry import AtomicToolRegistry


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_handler(name: str, provider: str = "test") -> ToolHandler:
    """Create a test tool handler with the given name."""
    return _TestHandler(
        definition=ToolDefinition(
            name=name,
            description=f"Test tool {name}",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=30.0,
            idempotent=True,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider=provider),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
        ),
    )


class _TestHandler:
    """Minimal handler for testing."""

    def __init__(self, definition: ToolDefinition) -> None:
        self._definition = definition

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        *,
        arguments: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        return {"result": "ok"}


# ── Tests ────────────────────────────────────────────────────────────────


class TestAtomicToolRegistry:
    async def test_empty_registry(self) -> None:
        """A fresh registry has version 0 and no tools."""
        registry = AtomicToolRegistry()
        snap = registry.snapshot()
        assert snap.version == 0
        assert snap.definitions == {}
        assert snap.handlers == {}

    async def test_replace_provider_adds_tools(self) -> None:
        """replace_provider_tools adds handlers and increments version."""
        registry = AtomicToolRegistry()
        handlers = [_make_handler("read_file"), _make_handler("list_dir")]

        snap = await registry.replace_provider_tools(
            provider_name="builtin",
            handlers=handlers,
        )

        assert snap.version >= 1
        assert "read_file" in snap.definitions
        assert "list_dir" in snap.definitions
        assert isinstance(snap.definitions["read_file"], ToolDefinition)

    async def test_initial_handlers(self) -> None:
        """Initial handlers passed at construction are registered."""
        handlers = [_make_handler("tool_a"), _make_handler("tool_b")]
        registry = AtomicToolRegistry(initial_handlers=handlers)

        snap = registry.snapshot()
        assert snap.version == 1
        assert "tool_a" in snap.definitions
        assert "tool_b" in snap.definitions

    async def test_resolve_handler_by_name(self) -> None:
        """resolve returns the correct handler."""
        registry = AtomicToolRegistry()
        handler = _make_handler("my_tool")
        await registry.replace_provider_tools(provider_name="test", handlers=[handler])

        resolved = registry.resolve("my_tool")
        assert resolved is handler

    async def test_resolve_raises_on_unknown(self) -> None:
        """resolve raises KeyError for unknown tool names."""
        registry = AtomicToolRegistry()
        with pytest.raises(KeyError, match="my_tool"):
            registry.resolve("my_tool")

    async def test_resolve_def_returns_none_for_unknown(self) -> None:
        """resolve_def returns None for unknown tool names."""
        registry = AtomicToolRegistry()
        assert registry.resolve_def("nonexistent") is None

    async def test_replace_provider_updates_existing(self) -> None:
        """Replacing a provider's tools updates them atomically."""
        registry = AtomicToolRegistry()
        old_handler = _make_handler("tool_a")
        await registry.replace_provider_tools(provider_name="p1", handlers=[old_handler])

        new_handler = _make_handler("tool_a")
        snap = await registry.replace_provider_tools(
            provider_name="p1",
            handlers=[new_handler, _make_handler("tool_b")],
        )

        assert "tool_a" in snap.definitions
        assert "tool_b" in snap.definitions
        assert snap.version == 2

    async def test_replace_provider_multiple_providers(self) -> None:
        """Multiple providers can co-exist with different tool names."""
        registry = AtomicToolRegistry()
        await registry.replace_provider_tools(
            provider_name="builtin",
            handlers=[_make_handler("read_file")],
        )
        await registry.replace_provider_tools(
            provider_name="mcp",
            handlers=[_make_handler("mcp_search")],
        )

        snap = registry.snapshot()
        assert "read_file" in snap.definitions
        assert "mcp_search" in snap.definitions

    async def test_snapshot_is_immutable_copy(self) -> None:
        """Snapshot returns a copy—mutating it doesn't affefct registry."""
        registry = AtomicToolRegistry()
        handlers = [_make_handler("tool_x")]
        await registry.replace_provider_tools(provider_name="test", handlers=handlers)

        snap1 = registry.snapshot()
        snap2 = registry.snapshot()

        assert snap1.version == snap2.version
        assert snap1.definitions == snap2.definitions
        assert snap1.definitions is not snap2.definitions

    async def test_validate_definition_name(self) -> None:
        """Names must match ^[a-z][a-z0-9_]{0,63}$."""
        registry = AtomicToolRegistry()

        for invalid_name in ("Tool", "123abc", "tool-name", "tool name", ""):
            with pytest.raises((ValueError, TypeError)):
                await registry.replace_provider_tools(
                    provider_name="test",
                    handlers=[_make_handler(invalid_name)],
                )

    async def test_validate_definition_schema_type(self) -> None:
        """Schema root type must be 'object'."""
        registry = AtomicToolRegistry()

        handler = _TestHandler(
            definition=ToolDefinition(
                name="bad_schema",
                description="Bad schema",
                input_schema={"type": "string"},  # Must be 'object'
                side_effect=ToolSideEffect.NONE,
                risk_level=ToolRiskLevel.LOW,
                timeout_seconds=30.0,
                idempotent=True,
                parallel_safe=True,
            ),
        )

        with pytest.raises(ValueError, match="input_schema root type must be 'object'"):
            await registry.replace_provider_tools(provider_name="test", handlers=[handler])

    async def test_resolve_from_list(self) -> None:
        """resolve_from_list matches against an explicit list."""
        registry = AtomicToolRegistry()
        defn = _make_handler("tool_a").definition

        # When tool exists in list
        result = registry.resolve_from_list(
            name="tool_a",
            available_tools=(defn,),
        )
        assert result is defn

        # When tool doesn't exist in list
        result = registry.resolve_from_list(
            name="tool_b",
            available_tools=(defn,),
        )
        assert result is None

    async def test_snapshot_created_at(self) -> None:
        """Snapshot has a valid created_at timestamp."""
        registry = AtomicToolRegistry()
        await registry.replace_provider_tools(
            provider_name="test",
            handlers=[_make_handler("tool_a")],
        )

        snap = registry.snapshot()
        assert snap.created_at is not None
        assert isinstance(snap.created_at, datetime)
