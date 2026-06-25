# cogito/agent/__init__.py

"""Cogito Agent — Runtime kernel, phase pipeline, ports, and application service.

This package implements the Agent Runtime framework described in
cogito-agent-initial-framework-spec.md. It provides:

- RuntimeKernel with a fixed-order phase pipeline.
- 8 default phases (TurnInit → ... → TurnFinalize).
- Strongly typed TurnContext, AgentRequest, TurnResult, and AgentEvent.
- Protocol-based Port interfaces for all infrastructure boundaries.
- AgentApplicationService as the stable entry point.
- Messaging stubs (MessageEnvelope, Worker, Mapper) for future MessageBus integration.

Architecture constraints:
- Kernel does NOT import Channel SDK types.
- Kernel does NOT import MessageBus implementations.
- Phases are exclusively ordered by the composition root list.
- No topological sort, no requires/produces, no auto-discovery.
"""

from cogito.agent.application import AgentApplicationService
from cogito.agent.bootstrap import build_runtime_kernel, build_test_kernel
from cogito.agent.runtime import (
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AttachmentRef,
    DuplicatePhaseNameError,
    InvalidTurnStateError,
    MissingTurnResultError,
    PhaseNotImplementedError,
    RuntimeAgentError,
    TurnContext,
    TurnResult,
    TurnStatus,
)

__all__ = [
    "AgentApplicationService",
    "AgentEvent",
    "AgentEventType",
    "AgentRequest",
    "AttachmentRef",
    "DuplicatePhaseNameError",
    "InvalidTurnStateError",
    "MissingTurnResultError",
    "PhaseNotImplementedError",
    "RuntimeAgentError",
    "TurnContext",
    "TurnResult",
    "TurnStatus",
    "build_runtime_kernel",
    "build_test_kernel",
]
