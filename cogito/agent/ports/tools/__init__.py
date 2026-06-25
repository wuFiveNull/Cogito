# cogito/agent/ports/tools/__init__.py
#
# Tool subsystem ports — all Protocol interfaces for the tool orchestration
# layer.
#
# This __init__.py re-exports the legacy port types from tools_backward.py
# so that existing imports from ``cogito.agent.ports.tools`` continue
# to work without modification, alongside the new spec-complete port
# interfaces from the sub-modules below.

from cogito.agent.ports.tools.approval import ToolApprovalCoordinatorPort
from cogito.agent.ports.tools.artifacts import ToolArtifactStorePort
from cogito.agent.ports.tools.audit import ToolAuditPort
from cogito.agent.ports.tools.catalog import ToolCatalogPort, ToolSelectionRequest, VisibleToolSet
from cogito.agent.ports.tools.checkpoint import ToolLoopCheckpointPort
from cogito.agent.ports.tools.executor import ToolExecutionContext, ToolExecutorPort
from cogito.agent.ports.tools.policy import ToolPolicyPort, ToolPolicyDecision, ToolPolicyDecisionType
from cogito.agent.ports.tools.rate_limit import ToolRateLimiterPort
from cogito.agent.ports.tools.registry import (
    ToolConflictPolicy,
    ToolHandler,
    ToolProvider,
    ToolRegistryPort as NewToolRegistryPort,
    ToolRegistrySnapshot,
    StreamingToolHandler,
)
from cogito.agent.ports.tools.sandbox import ToolSandboxPort, WorkspaceScopePort

# ── Legacy backward-compat imports ────────────────────────────────────
# These are the original port types used by the existing AgentLoopPhase
# and related code.  They are re-exported here so that existing imports
# from ``cogito.agent.ports.tools`` continue to resolve.

from cogito.agent.ports.tools_backward import (
    ToolRegistryPort,
)

__all__ = [
    "NewToolRegistryPort",
    "StreamingToolHandler",
    "ToolApprovalCoordinatorPort",
    "ToolArtifactStorePort",
    "ToolAuditPort",
    "ToolCatalogPort",
    "ToolConflictPolicy",
    "ToolExecutionContext",
    "ToolExecutorPort",
    "ToolHandler",
    "ToolLoopCheckpointPort",
    "ToolPolicyDecision",
    "ToolPolicyDecisionType",
    "ToolPolicyPort",
    "ToolProvider",
    "ToolRateLimiterPort",
    "ToolRegistryPort",
    "ToolRegistrySnapshot",
    "ToolSandboxPort",
    "ToolSelectionRequest",
    "ToolWorkspaceScopePort",
    "VisibleToolSet",
]
