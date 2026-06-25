"""Tests for DefaultToolCatalog, HybridToolSelector, and related components."""

from __future__ import annotations

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
from cogito.agent.ports.tools.catalog import ToolSelectionRequest, VisibleToolSet
from cogito.agent.tools.catalog import DefaultToolCatalog, ToolsetConfig
from cogito.agent.tools.registry import AtomicToolRegistry
from cogito.agent.tools.selector import HybridToolSelector


def _make_def(name: str, kind: ToolKind = ToolKind.READ, risk: ToolRisk = ToolRisk.READ_ONLY,
              tags: frozenset[str] | None = None, always_visible: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool: {name}",
        input_schema={"type": "object", "properties": {}},
        side_effect=ToolSideEffect.NONE,
        risk_level=ToolRiskLevel.LOW,
        timeout_seconds=30.0,
        idempotent=True,
        parallel_safe=True,
        kind=kind,
        risk=risk,
        source=ToolSource(type=ToolSourceType.BUILTIN, provider="test"),
        tags=tags or frozenset(),
        always_visible=always_visible,
    )


def _make_handler(defn: ToolDefinition) -> object:
    """Create a minimal handler wraper."""
    class _Handler:
        @property
        def definition(self) -> ToolDefinition:
            return defn
        async def execute(self, **kwargs):
            return {"result": "ok"}
    return _Handler()


class TestHybridToolSelector:
    def test_selects_tools_by_query(self) -> None:
        """Query relevance selects matching tools."""
        selector = HybridToolSelector()
        candidates = [
            _make_def("read_file", tags=frozenset({"file", "read"})),
            _make_def("list_dir", tags=frozenset({"file", "dir"})),
            _make_def("shell", risk=ToolRisk.PRIVILEGED),
        ]

        selected, deferred, reasons = selector.select(
            candidates=candidates,
            query="read",
            model_max_tools=10,
            always_visible=frozenset(),
        )

        names = [d.name for d in selected]
        assert "read_file" in names

    def test_model_max_tools_caps_selection(self) -> None:
        """model_max_tools limits the number of selected tools."""
        selector = HybridToolSelector()
        candidates = [_make_def(f"tool_{i}") for i in range(20)]

        selected, deferred, reasons = selector.select(
            candidates=candidates,
            query="",
            model_max_tools=5,
            always_visible=frozenset(),
        )

        assert len(selected) <= 5
        assert len(deferred) >= 15

    def test_always_visible_included(self) -> None:
        """Always-visible tools are always selected."""
        selector = HybridToolSelector()
        candidates = [
            _make_def("always_tool", always_visible=True),
            _make_def("other_tool"),
        ]

        selected, deferred, reasons = selector.select(
            candidates=candidates,
            query="",
            model_max_tools=1,
            always_visible=frozenset({"always_tool"}),
        )

        names = [d.name for d in selected]
        assert "always_tool" in names

    def test_lru_tracking(self) -> None:
        """Recently used tools get boosted."""
        selector = HybridToolSelector()

        selector.record_usage(session_id="session_1", tool_name="frequent_tool", weight=1.0)

        candidates = [
            _make_def("frequent_tool"),
            _make_def("rare_tool"),
        ]

        selected, deferred, reasons = selector.select(
            candidates=candidates,
            query="rare",
            model_max_tools=2,
            always_visible=frozenset(),
        )

        # Both should be selected
        assert len(selected) == 2


class TestDefaultToolCatalog:
    async def test_select_with_no_toolsets(self) -> None:
        """Empty toolsets return no tools."""
        registry = AtomicToolRegistry()
        selector = HybridToolSelector()
        catalog = DefaultToolCatalog(registry=registry, selector=selector)

        # Register a tool
        handler = _make_handler(_make_def("read_file"))
        await registry.replace_provider_tools(provider_name="test", handlers=[handler])

        # Select without a toolset that includes this tool
        result = await catalog.select(
            ToolSelectionRequest(
                actor_id="actor_1",
                session_id="session_1",
                query="",
                requested_toolsets=("nonexistent",),
                model_id="gpt-4",
                model_max_tools=10,
                registry_version=registry.snapshot().version,
                allowed_risks=frozenset({ToolRisk.READ_ONLY}),
            ),
        )

        assert len(result.definitions) == 0
        assert result.registry_version >= 1

    async def test_toolset_resolution(self) -> None:
        """Toolsets resolve correctly with include/exclude."""
        registry = AtomicToolRegistry()
        selector = HybridToolSelector()

        handler_a = _make_handler(_make_def("tool_a"))
        handler_b = _make_handler(_make_def("tool_b"))
        handler_c = _make_handler(_make_def("tool_c"))
        await registry.replace_provider_tools(
            provider_name="test",
            handlers=[handler_a, handler_b, handler_c],
        )

        catalog = DefaultToolCatalog(
            registry=registry,
            selector=selector,
            toolsets={
                "core": ToolsetConfig(
                    include=frozenset({"tool_a", "tool_b"}),
                ),
            },
        )

        result = await catalog.select(
            ToolSelectionRequest(
                actor_id="actor_1",
                session_id="session_1",
                query="",
                requested_toolsets=("core",),
                model_id="gpt-4",
                model_max_tools=10,
                registry_version=registry.snapshot().version,
                allowed_risks=frozenset({ToolRisk.READ_ONLY}),
            ),
        )

        names = [d.name for d in result.definitions]
        assert "tool_a" in names
        assert "tool_b" in names
        assert "tool_c" not in names

    async def test_toolset_cycle_detection(self) -> None:
        """Circular toolset references are detected."""
        registry = AtomicToolRegistry()
        selector = HybridToolSelector()

        with pytest.raises(ValueError, match="Circular"):
            DefaultToolCatalog(
                registry=registry,
                selector=selector,
                toolsets={
                    "a": ToolsetConfig(include_sets=frozenset({"b"})),
                    "b": ToolsetConfig(include_sets=frozenset({"a"})),
                },
            )

    async def test_toolset_missing_reference(self) -> None:
        """Missing toolset references are detected."""
        registry = AtomicToolRegistry()
        selector = HybridToolSelector()

        with pytest.raises(ValueError, match="unknown"):
            DefaultToolCatalog(
                registry=registry,
                selector=selector,
                toolsets={
                    "a": ToolsetConfig(include_sets=frozenset({"unknown_set"})),
                },
            )
