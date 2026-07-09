"""Capability Registry 2.0 — Plan 03 M1."""
from __future__ import annotations

import pytest

from cogito.capability.models import ToolDef
from cogito.capability.registry import CapabilityRegistry


def _make_tool(name: str, **kw: Any) -> ToolDef:
    return ToolDef(
        name=name, description=f"tool {name}", input_schema={"type": "object"},
        handler=lambda args, ctx: "", **kw,
    )


def test_capability_id_format() -> None:
    """capability_id = namespace:name。"""
    t = _make_tool("echo", namespace="core")
    assert t.capability_id == "core:echo"


def test_registry_registers_and_gets() -> None:
    reg = CapabilityRegistry()
    t = _make_tool("echo")
    reg.register(t)
    assert reg.get("core:echo") is not None


def test_disabled_tool_excluded_from_snapshot() -> None:
    """disabled 工具不会进入 Model Schema。"""
    reg = CapabilityRegistry()
    reg.register(_make_tool("active"))
    reg.register(_make_tool("old", disabled=True))
    snap = reg.build_snapshot()
    ids = snap.capability_ids
    assert "core:active" in ids
    assert "core:old" not in ids


def test_deprecated_tool_excluded() -> None:
    """deprecated 工具排除。"""
    reg = CapabilityRegistry()
    reg.register(_make_tool("live"))
    reg.register(_make_tool("legacy", deprecated=True))
    snap = reg.build_snapshot()
    assert "core:legacy" not in snap.capability_ids


def test_snapshot_filters_by_toolset() -> None:
    """按 toolset 过滤。"""
    reg = CapabilityRegistry()
    reg.register(_make_tool("a", toolset=("core",)))
    reg.register(_make_tool("b", toolset=("proactive",)))
    snap = reg.build_snapshot(toolsets={"core"})
    ids = snap.capability_ids
    assert "core:a" in ids
    assert "core:b" not in ids


def test_snapshot_filters_by_mode() -> None:
    """proactive 模式看不到 terminal/code_exec。"""
    reg = CapabilityRegistry()
    reg.register(_make_tool("safe", supported_modes=("proactive", "normal")))
    reg.register(_make_tool("term", supported_modes=("normal",)))
    snap = reg.build_snapshot(mode="proactive")
    ids = snap.capability_ids
    assert "core:safe" in ids
    assert "core:term" not in ids


def test_snapshot_stable_order() -> None:
    """Tool Schema 顺序稳定（便于缓存和回放）。"""
    reg = CapabilityRegistry()
    for name in ["z", "a", "m", "b"]:
        reg.register(_make_tool(name))
    snap1 = reg.build_snapshot()
    snap2 = reg.build_snapshot()
    assert snap1.capability_ids == snap2.capability_ids
    assert snap1.capability_ids == tuple(sorted(snap1.capability_ids))


def test_health_check_filters_unhealthy() -> None:
    """check_fn 返回 False 的工具被过滤。"""
    reg = CapabilityRegistry()
    reg.register(_make_tool("healthy", check_fn=lambda: True))
    reg.register(_make_tool("sick", check_fn=lambda: False))
    snap = reg.build_snapshot()
    ids = snap.capability_ids
    assert "core:healthy" in ids
    assert "core:sick" not in ids


def test_side_effect_class_metadata() -> None:
    """side_effect_class 元数据完整。"""
    t = _make_tool("send", side_effect_class="reconcilable")
    assert t.side_effect_class == "reconcilable"


from typing import Any  # noqa: E402