# cogito/agent/runtime/kernel.py
#
# RuntimeKernel — fixed-order phase pipeline for agent turn execution.
#
# The kernel orchestrates a sequence of phases in order. It is:
# - Channel-agnostic: only accepts AgentRequest.
# - MessageBus-agnostic: only emits AgentEvent / TurnResult.
# - Extensible: phases are injected at construction; add more by
#   modifying the composition root.
#
# Design rules (see initial-framework-spec §10, agent-loop-spec §23.1):
#   - Phase order is defined by a single explicit list, never by
#     topological sorting or implicit scanning.
#   - Kernel does not branch on phase.name.
#   - Kernel creates and injects a TurnEventEmitter bound to the turn.
#   - Turn status after completion is read from TurnResult, not
#     unconditionally set to COMPLETED (supports WAITING_APPROVAL).

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Mapping

from cogito.agent.ports.events import AgentEventSink, NullAgentEventSink
from cogito.agent.runtime.cleanup import RuntimeCleanup
from cogito.agent.runtime.context import CancellationToken, TurnContext, TurnEventEmitter
from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.errors import (
    DefaultRuntimeErrorMapper,
    DuplicatePhaseNameError,
    InvalidTurnStateError,
    MissingTurnResultError,
    RuntimeAgentError,
    RuntimeErrorMapper,
)
from cogito.agent.runtime.events import AgentEvent, AgentEventType
from cogito.agent.runtime.models import AgentRequest, TurnResult, TurnStatus
from cogito.agent.runtime.phase import RuntimePhase

logger = logging.getLogger(__name__)

# Sentinel for missing deadline
_UNDEFINED_DEADLINE: datetime = datetime(1, 1, 1)


# ── Default TurnEventEmitter ────────────────────────────────────────────


class DefaultTurnEventEmitter:
    """Safe event emitter bound to one turn context.

    Responsible for:
      - Auto-filling turn_id, request_id, timestamp.
      - Isolating EventSink failures (must not crash the turn).
      - Filtering forbidden fields (no Exceptions, no SDK objects).

    This is the ONLY emitter phase code should use.  Phases must never
    hold a raw AgentEventSink reference.
    """

    __slots__ = ("_sink", "_turn_id", "_request_id")

    def __init__(self, *, sink: AgentEventSink, turn_id: str, request_id: str) -> None:
        self._sink = sink
        self._turn_id = turn_id
        self._request_id = request_id

    async def emit(
        self,
        event_type: AgentEventType,
        *,
        phase: str | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        event = AgentEvent(
            type=event_type,
            turn_id=self._turn_id,
            request_id=self._request_id,
            timestamp=datetime.now(),
            phase=phase,
            data=data or {},
        )
        try:
            await self._sink.emit(event)
        except Exception:
            logger.exception(
                "Agent event delivery failed",
                extra={
                    "event_type": event_type,
                    "turn_id": self._turn_id,
                    "request_id": self._request_id,
                },
            )


# ── RuntimeKernel ───────────────────────────────────────────────────────


class RuntimeKernel:
    """Fixed-order phase pipeline for agent turn execution."""

    def __init__(
        self,
        phases: Sequence[RuntimePhase],
        *,
        context_factory: TurnContextFactory,
        default_event_sink: AgentEventSink | None = None,
        cleanup: RuntimeCleanup | None = None,
        error_mapper: RuntimeErrorMapper | None = None,
    ) -> None:
        self._validate_unique_names(phases)
        self._phases = list(phases)
        self._context_factory = context_factory
        self._default_event_sink = default_event_sink or NullAgentEventSink()
        self._cleanup = cleanup
        self._error_mapper = error_mapper or DefaultRuntimeErrorMapper()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        request: AgentRequest,
        *,
        event_sink: AgentEventSink | None = None,
    ) -> TurnResult:
        sink = event_sink or self._default_event_sink
        ctx = self._context_factory.create(request)

        # Inject turn-level event emitter
        ctx.event_emitter = DefaultTurnEventEmitter(
            sink=sink,
            turn_id=ctx.turn_id or "",
            request_id=request.request_id,
        )

        async def emit_safely(event: AgentEvent) -> None:
            try:
                await sink.emit(event)
            except Exception:
                logger.exception(
                    "Agent event delivery failed",
                    extra={"event_type": event.type, "request_id": request.request_id},
                )

        try:
            await emit_safely(self._build_event(AgentEventType.TURN_STARTED, ctx))

            for phase in self._phases:
                ctx.current_phase = phase.name

                await emit_safely(
                    self._build_event(AgentEventType.PHASE_STARTED, ctx, phase=phase.name),
                )

                try:
                    await phase.run(ctx)
                except Exception as exc:
                    await emit_safely(
                        self._build_event(
                            AgentEventType.PHASE_FAILED,
                            ctx,
                            phase=phase.name,
                            data={"error_code": getattr(exc, "code", "UNKNOWN")},
                        ),
                    )
                    raise

                await emit_safely(
                    self._build_event(
                        AgentEventType.PHASE_COMPLETED,
                        ctx,
                        phase=phase.name,
                    ),
                )

            if ctx.result is None:
                raise MissingTurnResultError(
                    "No TurnResult was produced after all phases completed.",
                )

            # Use the result's status instead of hardcoding COMPLETED
            # (supports WAITING_APPROVAL, DENIED, etc. — see agent-loop-spec §23.1)
            ctx.status = ctx.result.status

            if ctx.status is TurnStatus.COMPLETED:
                await emit_safely(self._build_event(AgentEventType.TURN_COMPLETED, ctx))
            elif ctx.status is TurnStatus.WAITING_APPROVAL:
                await emit_safely(self._build_event(AgentEventType.TURN_SUSPENDED, ctx))
            elif ctx.status is TurnStatus.DENIED:
                await emit_safely(self._build_event(AgentEventType.TURN_COMPLETED, ctx))
            else:
                raise InvalidTurnStateError(
                    f"Unsupported terminal turn status: {ctx.status}",
                    safe_message="Agent turn ended in an unexpected state",
                )

            # Defensive consistency check
            if ctx.result.status is not ctx.status:
                raise InvalidTurnStateError(
                    "TurnResult status does not match TurnContext status",
                    safe_message="Agent turn result is inconsistent",
                )

            return ctx.result

        except asyncio.CancelledError:
            ctx.status = TurnStatus.CANCELLED
            raise

        except Exception as exc:
            ctx.status = TurnStatus.FAILED
            ctx.error = exc

            mapped = self._error_mapper.map(exc)

            await emit_safely(
                self._build_event(
                    AgentEventType.TURN_FAILED,
                    ctx,
                    data={
                        "error_code": mapped.code,
                        "safe_message": mapped.safe_message,
                        "retryable": mapped.retryable,
                    },
                ),
            )

            if isinstance(exc, RuntimeAgentError):
                raise

            raise RuntimeAgentError(
                mapped.safe_message,
                safe_message=mapped.safe_message,
            ) from exc

        finally:
            if self._cleanup is not None:
                try:
                    await self._cleanup.run(ctx)
                except Exception:
                    logger.exception(
                        "RuntimeCleanup failed",
                        extra={"turn_id": ctx.turn_id, "request_id": ctx.request.request_id},
                    )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_unique_names(phases: Sequence[RuntimePhase]) -> None:
        seen: set[str] = set()
        for phase in phases:
            name = phase.name
            if name in seen:
                raise DuplicatePhaseNameError(
                    f"Duplicate phase name: {name!r}. "
                    "Each phase must have a unique name.",
                )
            seen.add(name)

    @staticmethod
    def _build_event(
        event_type: AgentEventType,
        ctx: TurnContext,
        *,
        phase: str | None = None,
        data: dict[str, object] | None = None,
    ) -> AgentEvent:
        return AgentEvent(
            type=event_type,
            turn_id=ctx.turn_id or "",
            request_id=ctx.request.request_id,
            timestamp=datetime.now(),
            phase=phase,
            data=data or {},
        )
