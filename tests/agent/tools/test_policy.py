"""Tests for CompositeToolPolicyEngine — layered risk-based policy."""

from __future__ import annotations

import pytest

from cogito.agent.domain.tools import (
    ToolDefinition,
    ToolKind,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)
from cogito.agent.ports.tools.policy import (
    ToolPolicyDecisionType,
    ToolPolicyRequest,
)
from cogito.infrastructure.tools.policy_engine import CompositeToolPolicyEngine


def _make_def(
    name: str = "test_tool",
    risk: ToolRisk = ToolRisk.READ_ONLY,
    side_effect: ToolSideEffect = ToolSideEffect.NONE,
) -> ToolDefinition:
    return ToolDefinition(
        name=name, description="test",
        input_schema={"type": "object", "properties": {}},
        side_effect=side_effect, risk_level=ToolRiskLevel.LOW,
        timeout_seconds=30.0, idempotent=True, parallel_safe=True,
        kind=ToolKind.READ, risk=risk,
        source=ToolSource(type=ToolSourceType.BUILTIN, provider="test"),
        tags=frozenset(), always_visible=True,
    )


class TestCompositeToolPolicyEngine:
    async def test_read_only_allowed(self) -> None:
        engine = CompositeToolPolicyEngine()
        request = ToolPolicyRequest(
            definition=_make_def(risk=ToolRisk.READ_ONLY),
            arguments={}, actor_id="a1", session_id="s1",
            workspace_id=None, channel_capabilities=frozenset(),
        )
        d = await engine.evaluate(request)
        assert d.decision is ToolPolicyDecisionType.ALLOW

    async def test_local_write_allowed(self) -> None:
        engine = CompositeToolPolicyEngine()
        request = ToolPolicyRequest(
            definition=_make_def(risk=ToolRisk.LOCAL_WRITE),
            arguments={}, actor_id="a1", session_id="s1",
            workspace_id=None, channel_capabilities=frozenset(),
        )
        d = await engine.evaluate(request)
        assert d.decision is ToolPolicyDecisionType.ALLOW

    async def test_external_write_requires_approval(self) -> None:
        engine = CompositeToolPolicyEngine()
        request = ToolPolicyRequest(
            definition=_make_def(risk=ToolRisk.EXTERNAL_WRITE),
            arguments={}, actor_id="a1", session_id="s1",
            workspace_id=None, channel_capabilities=frozenset(),
        )
        d = await engine.evaluate(request)
        assert d.decision is ToolPolicyDecisionType.REQUIRE_APPROVAL

    async def test_privileged_denied(self) -> None:
        engine = CompositeToolPolicyEngine()
        request = ToolPolicyRequest(
            definition=_make_def(risk=ToolRisk.PRIVILEGED),
            arguments={}, actor_id="a1", session_id="s1",
            workspace_id=None, channel_capabilities=frozenset(),
        )
        d = await engine.evaluate(request)
        assert d.decision is ToolPolicyDecisionType.DENY

    async def test_external_mutation_requires_approval(self) -> None:
        engine = CompositeToolPolicyEngine()
        request = ToolPolicyRequest(
            definition=_make_def(risk=ToolRisk.READ_ONLY, side_effect=ToolSideEffect.EXTERNAL_MUTATION),
            arguments={}, actor_id="a1", session_id="s1",
            workspace_id=None, channel_capabilities=frozenset(),
        )
        d = await engine.evaluate(request)
        assert d.decision is ToolPolicyDecisionType.REQUIRE_APPROVAL

    async def test_prior_grant_allows(self) -> None:
        class FakeGrant:
            tool_name = "test_tool"
        engine = CompositeToolPolicyEngine()
        request = ToolPolicyRequest(
            definition=_make_def(risk=ToolRisk.EXTERNAL_WRITE),
            arguments={}, actor_id="a1", session_id="s1",
            workspace_id=None, channel_capabilities=frozenset(),
            prior_grants=(FakeGrant(),),
        )
        d = await engine.evaluate(request)
        assert d.decision is ToolPolicyDecisionType.ALLOW

    async def test_safe_message_included(self) -> None:
        engine = CompositeToolPolicyEngine()
        request = ToolPolicyRequest(
            definition=_make_def(risk=ToolRisk.PRIVILEGED),
            arguments={}, actor_id="a1", session_id="s1",
            workspace_id=None, channel_capabilities=frozenset(),
        )
        d = await engine.evaluate(request)
        assert d.safe_message
