# tests/agent/architecture/test_dependency_boundaries.py

"""
Architecture boundary tests.

These tests verify that the runtime layer does not import
forbidden modules (MessageBus, Channel SDKs, etc.)
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

# Modules that the runtime package must never import
FORBIDDEN_RUNTIME_IMPORTS = [
    "redis",
    "nats",
    "kafka",
    "rabbitmq",
    "telegram",
    "discord",
    "fastapi",
    "starlette",
]

# Runtime package paths under test
RUNTIME_PACKAGES = [
    "cogito.agent.runtime",
    "cogito.agent.runtime.kernel",
    "cogito.agent.runtime.context",
    "cogito.agent.runtime.events",
    "cogito.agent.runtime.models",
    "cogito.agent.runtime.phase",
    "cogito.agent.runtime.phases.turn_init",
    "cogito.agent.runtime.phases.state_load",
    "cogito.agent.runtime.phases.information_retrieval",
    "cogito.agent.runtime.phases.context_assembly",
    "cogito.agent.runtime.phases.agent_loop",
    "cogito.agent.runtime.phases.knowledge_extraction",
    "cogito.agent.runtime.phases.persistence",
    "cogito.agent.runtime.phases.turn_finalize",
]

DOMAIN_PACKAGES = [
    "cogito.agent.domain.messages",
    "cogito.agent.domain.model_input",
    "cogito.agent.domain.retrieval",
    "cogito.agent.domain.preferences",
    "cogito.agent.domain.memory",
    "cogito.agent.domain.state",
]

PACKAGES_UNDER_TEST = RUNTIME_PACKAGES + DOMAIN_PACKAGES


def _get_imports(module_name: str) -> set[str]:
    """Extract the set of top-level packages a module imports."""
    try:
        mod = importlib.import_module(module_name)
    except ImportError as exc:
        pytest.skip(f"Cannot import {module_name}: {exc}")
        return set()

    imports: set[str] = set()
    for name in dir(mod):
        if name.startswith("_"):
            continue
        if name in FORBIDDEN_RUNTIME_IMPORTS:
            imports.add(name)

    # Also check the source file directly via AST for imported names
    src_file = getattr(mod, "__file__", None)
    if src_file is None:
        return imports

    try:
        import ast

        with open(src_file) as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in FORBIDDEN_RUNTIME_IMPORTS:
                        imports.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    if top in FORBIDDEN_RUNTIME_IMPORTS:
                        imports.add(top)
    except Exception:
        pass

    return imports


@pytest.mark.parametrize("module_name", PACKAGES_UNDER_TEST)
def test_runtime_does_not_import_forbidden_modules(module_name: str) -> None:
    """Runtime/Domain modules must not import Channel SDK or MessageBus packages."""
    forbidden_found = _get_imports(module_name)
    assert not forbidden_found, (
        f"{module_name} imports forbidden packages: {forbidden_found}"
    )


def test_agent_request_has_no_channel_types() -> None:
    """AgentRequest must not contain Channel SDK types."""
    from cogito.agent.runtime.models import AgentRequest

    # AgentRequest should be a simple dataclass with basic fields
    assert hasattr(AgentRequest, "request_id")
    assert hasattr(AgentRequest, "session_id")
    assert hasattr(AgentRequest, "actor_id")
    assert hasattr(AgentRequest, "text")

    # No channel-related fields
    forbidden_fields = {"telegram", "discord", "http", "websocket", "channel"}
    for field_name in AgentRequest.__dataclass_fields__:
        assert field_name not in forbidden_fields, (
            f"AgentRequest contains channel field: {field_name}"
        )


def test_turn_context_has_no_channel_types() -> None:
    """TurnContext must not contain Channel SDK types."""
    from cogito.agent.runtime.context import TurnContext

    forbidden_fields = {"telegram", "discord", "http", "websocket"}
    for field_name in TurnContext.__dataclass_fields__:
        assert field_name not in forbidden_fields, (
            f"TurnContext contains channel field: {field_name}"
        )


def test_runtime_kernel_has_no_channel_imports() -> None:
    """Verify by scanning the kernel source for channel keywords."""
    kernel_path = Path(__file__).parents[3] / "cogito" / "agent" / "runtime" / "kernel.py"

    assert kernel_path.is_file(), f"kernel.py not found at {kernel_path}"

    source = kernel_path.read_text(encoding="utf-8")
    forbidden = ["telegram", "discord", "redis", "nats", "kafka", "rabbitmq"]

    for keyword in forbidden:
        assert keyword not in source.lower(), (
            f"Kernel source contains forbidden reference: {keyword}"
        )


def test_application_service_does_not_import_messagebus() -> None:
    """AgentApplicationService must not import MessageBus implementation types."""
    import ast

    service_path = (
        Path(__file__).parents[3]
        / "cogito"
        / "agent"
        / "application"
        / "agent_service.py"
    )
    assert service_path.is_file()

    with open(service_path) as f:
        tree = ast.parse(f.read())

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])

    forbidden = {"redis", "nats", "kafka", "rabbitmq", "aiosqlite", "sqlalchemy"}
    found = imports & forbidden
    assert not found, (
        f"AgentApplicationService imports forbidden packages: {found}"
    )
