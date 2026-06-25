# cogito/agent/bootstrap/tool_factory.py
#
# ToolSystem bootstrap factory — wires all tool subsystem components.
#
# Design rules (see tool-system-spec §24):
#   - ToolSystem is a simple container, not a service locator.
#   - All dependencies are explicit.
#   - RuntimeKernel receives only ToolPort abstractions (catalog, executor).

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Mapping, Protocol

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
from cogito.agent.ports.tools.catalog import ToolCatalogPort, ToolSelectionRequest, VisibleToolSet
from cogito.agent.ports.tools.registry import (
    ToolConflictPolicy,
    ToolHandler,
    ToolProvider,
    ToolRegistryPort,
    ToolRegistrySnapshot,
)
from cogito.agent.tools.catalog import DefaultToolCatalog, ToolsetConfig
from cogito.agent.tools.concurrency import ToolConcurrencyController
from cogito.agent.tools.context_governor import ContextGovernor
from cogito.agent.tools.orchestrator import DefaultToolOrchestrator, OrchestratorConfig
from cogito.agent.tools.providers import BuiltinToolProvider
from cogito.agent.tools.builtin.filesystem import ReadFileHandler, ListDirHandler, ReadArtifactHandler
from cogito.agent.tools.builtin.file_edit import WriteFileHandler, EditFileHandler, ApplyPatchHandler
from cogito.agent.tools.builtin.search_tools import GlobSearchHandler, GrepSearchHandler
from cogito.agent.tools.builtin.time_tool import GetCurrentTimeHandler
from cogito.agent.tools.builtin.tool_search import ToolSearchHandler
from cogito.agent.tools.builtin.web import WebFetchHandler, WebSearchHandler
from cogito.agent.tools.builtin.memory_tools import RecallMemoryHandler, MemorizeHandler, ForgetMemoryHandler
from cogito.agent.tools.builtin.messaging import SendMessageHandler
from cogito.agent.tools.builtin.shell_tool import ShellHandler
from cogito.agent.tools.builtin.background_task import TaskOutputHandler, TaskStopHandler
from cogito.agent.tools.builtin.spawn import SpawnHandler, SpawnOutputHandler
from cogito.agent.tools.registry import AtomicToolRegistry
from cogito.agent.tools.result_processor import DefaultToolResultProcessor, ResultProcessorConfig
from cogito.agent.tools.selector import HybridToolSelector
from cogito.agent.tools.validation import JsonSchemaToolValidator, JsonSchemaValidationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolSystem:
    """Container for all tool subsystem components.

    RuntimeKernel receives only catalog and executor — not the
    full registry, providers, or sandbox.
    """
    registry: AtomicToolRegistry
    catalog: DefaultToolCatalog
    executor: DefaultToolOrchestrator
    providers: tuple[ToolProvider, ...]


def build_tool_config(
    *,
    default_timeout_seconds: float = 60.0,
    max_parallel_calls: int = 4,
    argument_max_bytes: int = 262_144,
    inline_soft_limit_chars: int = 12_000,
    inline_hard_limit_chars: int = 50_000,
    turn_total_limit_chars: int = 120_000,
) -> tuple[OrchestratorConfig, ResultProcessorConfig]:
    """Build default tool configuration objects."""
    return (
        OrchestratorConfig(
            default_tool_timeout_seconds=default_timeout_seconds,
            max_parallel_calls=max_parallel_calls,
            argument_max_bytes=argument_max_bytes,
        ),
        ResultProcessorConfig(
            inline_soft_limit_chars=inline_soft_limit_chars,
            inline_hard_limit_chars=inline_hard_limit_chars,
        ),
    )


def create_builtin_handlers(
    *,
    workspace_scope: object | None = None,
    network_policy: object | None = None,
    secret_redactor: object | None = None,
    registry: object | None = None,
    file_guard: object | None = None,
    evasion_guardian: object | None = None,
    subagent_manager: object | None = None,
) -> list[ToolHandler]:
    """Create all built-in tool handlers with optional dependencies.

    Returns a list of ToolHandler instances for all 20+ built-in tools.
    """
    from cogito.agent.ports.tools.registry import ToolHandler
    handlers: list[ToolHandler] = []

    # Meta tool (needs registry to search)
    if registry is not None:
        handlers.append(ToolSearchHandler(registry=registry))

    # Filesystem tools (with file_guard protection)
    handlers.append(ReadFileHandler(workspace=workspace_scope, file_guard=file_guard))
    handlers.append(ListDirHandler(workspace=workspace_scope, file_guard=file_guard))
    handlers.append(WriteFileHandler(workspace=workspace_scope, file_guard=file_guard))
    handlers.append(EditFileHandler(workspace=workspace_scope, file_guard=file_guard))
    handlers.append(ApplyPatchHandler(workspace=workspace_scope, file_guard=file_guard))
    handlers.append(ReadArtifactHandler(artifact_store=None))

    # Search tools
    handlers.append(GlobSearchHandler(workspace=workspace_scope))
    handlers.append(GrepSearchHandler(workspace=workspace_scope))

    # Time
    handlers.append(GetCurrentTimeHandler())

    # Web (with SSRF protection)
    handlers.append(WebFetchHandler(network_policy=network_policy))
    handlers.append(WebSearchHandler(network_policy=network_policy))

    # Memory
    handlers.append(RecallMemoryHandler())
    handlers.append(MemorizeHandler())
    handlers.append(ForgetMemoryHandler())

    # Communication
    handlers.append(SendMessageHandler())

    # Shell (disabled by default, with evasion guardian)
    from cogito.infrastructure.sandbox.command_policy import CommandPolicy
    from cogito.infrastructure.sandbox.rule_engine import RuleEngine
    rule_engine = RuleEngine()
    rule_engine.load_builtin_rules()
    handlers.append(ShellHandler(
        command_policy=CommandPolicy(rule_engine=rule_engine),
        evasion_guardian=evasion_guardian,
        sandbox=None,
        enabled=False,
    ))

    # Background task tools
    handlers.append(TaskOutputHandler())
    handlers.append(TaskStopHandler())

    # Sub-agent / spawn tools
    handlers.append(SpawnHandler(subagent_manager=subagent_manager))
    handlers.append(SpawnOutputHandler(subagent_manager=subagent_manager))

    logger.info("Created %d built-in tool handlers", len(handlers))
    return handlers


async def build_tool_system(
    *,
    orchestrator_config: OrchestratorConfig | None = None,
    result_processor_config: ResultProcessorConfig | None = None,
    conflict_policy: ToolConflictPolicy = ToolConflictPolicy.ERROR,
    providers: Sequence[ToolProvider] | None = None,
    always_visible: frozenset[str] | None = None,
    toolsets: Mapping[str, ToolsetConfig] | None = None,
    artifact_store: object | None = None,
    workspace_scope: object | None = None,
    network_policy: object | None = None,
    secret_redactor: object | None = None,
) -> ToolSystem:
    """Build a fully-wired tool subsystem.

    Args:
        orchestrator_config: Execution pipeline config.
        result_processor_config: Result governance config.
        conflict_policy: Registry conflict resolution policy.
        providers: Tool providers (builtin, MCP, plugin, etc.).
        always_visible: Tool names that are always visible.
        toolsets: Named toolset configurations.
        artifact_store: Optional artifact store for large results.
        workspace_scope: Optional WorkspaceScopePort for path security.
        network_policy: Optional NetworkPolicy for SSRF protection.
        secret_redactor: Optional SecretRedactor for output redaction.

    Returns:
        A ToolSystem with registry, catalog, executor, and providers.
    """
    oc, rpc = build_tool_config()
    oc = orchestrator_config or oc
    rpc = result_processor_config or rpc

    # ── Create core components ──────────────────────────────────────────
    registry = AtomicToolRegistry(conflict_policy=conflict_policy)

    validator = JsonSchemaToolValidator()
    concurrency = ToolConcurrencyController()
    selector = HybridToolSelector()
    artifact_store_port = artifact_store
    result_processor = DefaultToolResultProcessor(
        artifact_store=artifact_store_port,
        config=rpc,
        redactor=secret_redactor,
    )

    # ── Load providers ──────────────────────────────────────────────────
    resolved_providers: list[ToolProvider] = list(providers) if providers else []

    # If no providers given, add a default builtin provider with all handlers
    if not resolved_providers:
        builtin_handlers = create_builtin_handlers(
            workspace_scope=workspace_scope,
            network_policy=network_policy,
            secret_redactor=secret_redactor,
            registry=registry,
        )
        resolved_providers = [BuiltinToolProvider(handlers=builtin_handlers)]

    for provider in resolved_providers:
        handlers = await provider.load()
        await registry.replace_provider_tools(
            provider_name=provider.name,
            handlers=handlers,
        )

    # ── Build catalog ───────────────────────────────────────────────────
    catalog = DefaultToolCatalog(
        registry=registry,
        selector=selector,
        toolsets=toolsets,
        always_visible=always_visible,
    )

    # ── Build orchestrator ──────────────────────────────────────────────
    executor = DefaultToolOrchestrator(
        registry=registry.snapshot(),
        validator=validator,
        result_processor=result_processor,
        concurrency=concurrency,
        config=oc,
    )

    return ToolSystem(
        registry=registry,
        catalog=catalog,
        executor=executor,
        providers=tuple(resolved_providers),
    )
