from __future__ import annotations

import pytest

from cogito.capability.models import ToolDef
from cogito.capability.registry import CapabilityRegistry
from cogito.model.router import ModelRouter
from cogito.model.stub_provider import StubModelProvider, StubScenario
from cogito.runtime.context import ContextItem, ContextSnapshot
from cogito.runtime.loop import AgentLoop


async def _handler(_args, _context) -> str:
    return "ok"


@pytest.mark.asyncio
async def test_read_only_child_snapshot_excludes_side_effect_tools() -> None:
    registry = CapabilityRegistry()
    read_tool = ToolDef(
        "read_file", "read", {"type": "object"}, _handler,
        toolset=("file",), side_effect_class="none",
    )
    write_tool = ToolDef(
        "write_file", "write", {"type": "object"}, _handler,
        toolset=("file",), side_effect_class="reconcilable",
    )
    registry.register(read_tool)
    registry.register(write_tool)
    provider = StubModelProvider([StubScenario(response_text="reviewed")])
    router = ModelRouter(providers={"stub": provider}, role_map={"main": "stub"})
    loop = AgentLoop(
        router,
        registry=registry,
        toolsets={"file"},
        policy_allowed_capabilities={read_tool.capability_id},
    )
    snapshot = ContextSnapshot(
        snapshot_id="snapshot",
        turn_id="turn",
        items=(
            ContextItem(
                item_type="message", item_id="message", source="test",
                role="user", content="review",
            ),
        ),
    )

    await loop.run(snapshot)

    names = {
        schema["function"]["name"]
        for schema in provider.received_requests[0].tools
    }
    assert names == {"read_file"}
