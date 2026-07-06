# cogito/agent/tools/concurrency.py
#
# ToolConcurrencyController — manages parallel/serial/exclusive execution.
#
# Design rules (see tool-system-spec §10.3):
#   - PARALLEL_SAFE + risk <= EXTERNAL_READ → may run in parallel.
#   - EXCLUSIVE → serialized globally.
#   - SERIAL_PER_TOOL → serialized per tool name.
#   - SERIAL_PER_SESSION → serialized per session.
#   - Parallel execution requires: same round, all PARALLEL_SAFE,
#     all risk <= EXTERNAL_READ, no dependency between calls.

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from cogito.agent.domain.tools import (
    ToolCall,
    ToolConcurrencyMode,
    ToolDefinition,
    ToolExecutionResult,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
)

logger = logging.getLogger(__name__)


class ToolConcurrencyController:
    """Controls concurrency for tool execution within a session.

    Uses asyncio.Semaphore per concurrency key to limit parallelism.
    """

    def __init__(self) -> None:
        # Per-key semaphores: key → Semaphore
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    # ── Public API ──────────────────────────────────────────────────────

    def can_parallel(
        self,
        definitions: list[ToolDefinition],
    ) -> bool:
        """Check if a batch of tools can execute in parallel.

        All must be PARALLEL_SAFE or have parallel_safe=True,
        risk <= EXTERNAL_READ, no side effects.
        """
        for d in definitions:
            if not d.parallel_safe:
                return False
            if d.concurrency_mode is not ToolConcurrencyMode.PARALLEL_SAFE:
                return False
            if d.side_effect is not ToolSideEffect.NONE:
                return False
            if d.risk_level is ToolRiskLevel.HIGH or d.risk_level is ToolRiskLevel.CRITICAL:
                return False
            if d.risk is ToolRisk.PRIVILEGED:
                return False
        return True

    async def acquire(
        self,
        *,
        definition: ToolDefinition,
        session_id: str,
    ) -> _ConcurrencyToken:
        """Acquire a concurrency slot for a tool call."""
        mode = definition.concurrency_mode

        if mode is ToolConcurrencyMode.EXCLUSIVE:
            lock = self._get_lock("exclusive")
            await lock.acquire()
            return _ConcurrencyToken("exclusive", lock)

        elif mode is ToolConcurrencyMode.SERIAL_PER_TOOL:
            lock = self._get_lock(f"tool:{definition.name}")
            await lock.acquire()
            return _ConcurrencyToken("serial_per_tool", lock)

        elif mode is ToolConcurrencyMode.SERIAL_PER_SESSION:
            lock = self._get_lock(f"session:{session_id}")
            await lock.acquire()
            return _ConcurrencyToken("serial_per_session", lock)

        elif mode is ToolConcurrencyMode.PARALLEL_SAFE:
            sem = self._get_semaphore("parallel", definition.limits.max_concurrency)
            await sem.acquire()
            return _ConcurrencyToken("parallel_safe", sem)

        return _ConcurrencyToken("parallel_safe", None)

    # ── Internal ────────────────────────────────────────────────────────

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _get_semaphore(self, key: str, max_concurrency: int) -> asyncio.Semaphore:
        if key not in self._semaphores:
            self._semaphores[key] = asyncio.Semaphore(max_concurrency)
        return self._semaphores[key]


class _ConcurrencyToken:
    """Token representing an acquired concurrency slot."""

    __slots__ = ("mode", "_resource")

    def __init__(self, mode: str, resource: object) -> None:
        self.mode = mode
        self._resource = resource

    async def release(self) -> None:
        if isinstance(self._resource, asyncio.Lock):
            self._resource.release()
        elif isinstance(self._resource, asyncio.Semaphore):
            self._resource.release()
