# cogito/agent/ports/model.py
#
# ModelPort — single unified stream-based model interface.
#
# Design rules (see agent-loop-phase-spec §7.1):
#   - Only exposes a single stream() method.
#   - Native-streaming providers map directly.
#   - Non-streaming providers wrap their response into a stream of events.
#   - AgentLoop never branches on "if model.supports_streaming".
#   - stream() returns AsyncIterator[ModelStreamEvent], never provider SDK types.

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from cogito.agent.domain.model import ModelInvocationRequest, ModelStreamEvent


class ModelPort(Protocol):
    """Abstract LLM interface — the only model API AgentLoop needs."""

    def stream(
        self,
        request: ModelInvocationRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        ...
