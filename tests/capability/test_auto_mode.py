from __future__ import annotations

import pytest

from cogito.capability.auto_mode import (
    AutoModeDecision,
    AutoModeGate,
    AutoModeResult,
)
from cogito.capability.executor import ToolExecutor
from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.policy import ToolPolicy
from cogito.capability.registry import CapabilityRegistry
from cogito.config import CapabilityConfig, ConfigError
from cogito.service.approval_service import SqliteApprovalService


async def _handler(args, context):
    return "executed"


class _Classifier:
    def __init__(self, result: AutoModeResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.requests = []

    async def classify(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        assert self.result is not None
        return self.result


def _tool(**overrides):
    values = {
        "name": "tool",
        "description": "test tool",
        "input_schema": {"type": "object"},
        "handler": _handler,
        "risk_level": "medium",
        "side_effect_class": "reconcilable",
    }
    values.update(overrides)
    return ToolDef(**values)


def _context(**overrides):
    values = {
        "attempt_id": "a1",
        "trace_id": "tr1",
        "tool_call_id": "tc1",
        "user_request": "please perform the requested operation",
    }
    values.update(overrides)
    return ToolContext(**values)


@pytest.mark.asyncio
async def test_low_risk_read_only_local_tool_uses_fast_path():
    classifier = _Classifier(error=AssertionError("classifier must not run"))
    gate = AutoModeGate(classifier)
    result = await gate.evaluate(
        _tool(risk_level="low", side_effect_class="none"),
        {},
        _context(),
    )
    assert result.is_allowed
    assert result.source == "fast_path"
    assert classifier.requests == []


@pytest.mark.asyncio
async def test_mcp_tool_does_not_use_local_fast_path():
    classifier = _Classifier(
        AutoModeResult(
            AutoModeDecision.block,
            "external action is unclear",
            "classifier_stage2",
        )
    )
    gate = AutoModeGate(classifier)
    result = await gate.evaluate(
        _tool(namespace="mcp", risk_level="low", side_effect_class="none"),
        {},
        _context(),
    )
    assert not result.is_allowed
    assert len(classifier.requests) == 1


@pytest.mark.asyncio
async def test_classifier_failure_is_fail_closed():
    gate = AutoModeGate(_Classifier(error=TimeoutError()))
    result = await gate.evaluate(_tool(), {}, _context())
    assert not result.is_allowed
    assert result.source == "unavailable"


@pytest.mark.asyncio
async def test_classifier_arguments_are_redacted():
    classifier = _Classifier(
        AutoModeResult(
            AutoModeDecision.allow,
            "authorized",
            "classifier_stage2",
        )
    )
    gate = AutoModeGate(classifier)
    await gate.evaluate(
        _tool(),
        {"api_key": "secret-value", "target": "calendar"},
        _context(),
    )
    assert classifier.requests[0].arguments["api_key"] == "<redacted>"
    assert classifier.requests[0].arguments["target"] == "calendar"


@pytest.mark.asyncio
async def test_deterministic_policy_deny_precedes_auto_mode():
    registry = CapabilityRegistry()
    registry.register(_tool(name="danger"))
    classifier = _Classifier(
        AutoModeResult(
            AutoModeDecision.allow,
            "allowed",
            "classifier_stage1",
        )
    )
    executor = ToolExecutor(
        registry,
        policy=ToolPolicy(denylist={"danger"}),
        auto_mode=AutoModeGate(classifier),
    )
    result = await executor.execute("tc1", "danger", {}, _context())
    assert result.status == "error"
    assert "policy denied" in result.error_message.lower()
    assert classifier.requests == []


@pytest.mark.asyncio
async def test_auto_mode_block_prevents_handler_execution():
    called = False

    async def handler(args, context):
        nonlocal called
        called = True
        return "executed"

    registry = CapabilityRegistry()
    registry.register(_tool(handler=handler))
    classifier = _Classifier(
        AutoModeResult(
            AutoModeDecision.block,
            "target is ambiguous",
            "classifier_stage2",
        )
    )
    executor = ToolExecutor(registry, auto_mode=AutoModeGate(classifier))
    result = await executor.execute("tc1", "tool", {}, _context())
    assert result.status == "error"
    assert "auto mode blocked" in result.error_message.lower()
    assert not called


@pytest.mark.asyncio
async def test_classifier_unavailable_creates_approval_when_service_exists(in_memory_db):
    in_memory_db.execute(
        "INSERT INTO turns(turn_id,status,created_at) VALUES ('turn','running',0)",
    )
    registry = CapabilityRegistry()
    registry.register(_tool())
    executor = ToolExecutor(
        registry,
        auto_mode=AutoModeGate(_Classifier(error=TimeoutError())),
        approval_service=SqliteApprovalService(in_memory_db),
    )

    result = await executor.execute(
        "tc-unavailable", "tool", {}, _context(turn_id="turn", principal_id="owner"),
    )

    assert result.status == "approval_required"
    assert result.approval_id


@pytest.mark.asyncio
async def test_auto_mode_allows_ordinary_idempotent_write():
    called = False

    async def handler(args, context):
        nonlocal called
        called = True
        return "written"

    registry = CapabilityRegistry()
    registry.register(_tool(handler=handler, side_effect_class="idempotent"))
    executor = ToolExecutor(
        registry,
        auto_mode=AutoModeGate(_Classifier(AutoModeResult(
            AutoModeDecision.allow, "ordinary write", "classifier_stage1",
        ))),
    )

    result = await executor.execute("tc-write", "tool", {}, _context())

    assert result.status == "success"
    assert called is True


def test_auto_mode_config_parsing_and_validation():
    config = CapabilityConfig._from_raw(
        {
            "auto_mode": {
                "enabled": True,
                "model_role": "fast",
                "safe_tools": ["core:now"],
                "max_argument_chars": 1024,
            },
        }
    )
    assert config.auto_mode.enabled
    assert config.auto_mode.safe_tools == ["core:now"]

    with pytest.raises(ConfigError):
        CapabilityConfig._from_raw(
            {
                "auto_mode": {"enabled": True, "max_argument_chars": 128},
            }
        )


def test_skill_root_is_explicit_and_not_derived_from_workspace():
    implicit = CapabilityConfig._from_raw({"workspace": {"root": "D:/work"}})
    explicit = CapabilityConfig._from_raw(
        {
            "workspace": {"root": "D:/work"},
            "skills": {"root": "D:/skills"},
        }
    )

    assert implicit.skills.root == ""
    assert explicit.skills.root == "D:/skills"


def test_shell_is_removed_and_stdio_mcp_requires_explicit_host_trust():
    with pytest.raises(ConfigError, match="Shell and process tools have been removed"):
        CapabilityConfig._from_raw({"shell": {"enabled": True}})
    with pytest.raises(ConfigError, match="explicit host_trusted"):
        CapabilityConfig._from_raw(
            {"mcp": {"servers": {"local": {"transport": "stdio", "command": "x"}}}}
        )

    configured = CapabilityConfig._from_raw(
        {
            "mcp": {
                "servers": {
                    "local": {
                        "transport": "stdio",
                        "command": "x",
                        "isolation": "host_trusted",
                    }
                }
            }
        }
    )
    assert configured.mcp_servers[0].isolation == "host_trusted"
