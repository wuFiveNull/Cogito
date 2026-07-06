# cogito/agent/runtime/phases/turn_finalize.py
#
# TurnFinalizePhase — Phase 8 of the 8-phase pipeline.
#
# Deterministic, side-effect-free result encapsulation phase.
#
# Design rules (see initial-framework-spec §4.8, agent-loop-spec §23.4):
#   - Validates preconditions (turn_id, output_text or valid status).
#   - Converges RUNNING → COMPLETED status.
#   - Preserves WAITING_APPROVAL and DENIED when set by earlier phases.
#   - Snapshots TurnContext into an immutable TurnResult.
#   - Applies a metadata allowlist — never copies the full ctx.metadata.
#
# It does NOT:
#   - Call any model, tool, repository, or MessageBus.
#   - Write to the database.
#   - Emit lifecycle events (handled by RuntimeKernel).
#   - Release resources (handled by RuntimeCleanup in ``finally``).

from __future__ import annotations

from collections.abc import Mapping

from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import InvalidTurnStateError
from cogito.agent.runtime.models import TurnResult, TurnStatus
from cogito.agent.runtime.phase import BasePhase


class TurnFinalizePhase(BasePhase):
    """Deterministic, side-effect-free result encapsulation phase.

    This phase is the last business phase in the pipeline. It:
    - Validates preconditions (turn_id, output_text, status).
    - Converges RUNNING → COMPLETED status.
    - Snapshots TurnContext into an immutable TurnResult.
    - Applies a metadata allowlist — never copies the full ctx.metadata.

    It does NOT:
    - Call any model, tool, repository, or MessageBus.
    - Write to the database.
    - Emit lifecycle events (handled by RuntimeKernel).
    - Release resources (handled by RuntimeCleanup in ``finally``).
    """

    name = "turn_finalize"

    _RESULT_METADATA_KEYS: frozenset[str] = frozenset({
        "finish_reason",
        "response_format",
        "output_language",
    })

    async def execute(self, ctx: TurnContext) -> None:
        turn_id = self._require_turn_id(ctx)
        final_status = self._resolve_final_status(ctx)
        output_text = self._resolve_output_text(ctx, final_status)

        candidate = TurnResult(
            turn_id=turn_id,
            request_id=ctx.request.request_id,
            session_id=ctx.request.session_id,
            actor_id=ctx.request.actor_id,
            status=final_status,
            text=output_text,
            usage=ctx.usage,
            tool_records=tuple(ctx.tool_records),
            metadata=self._build_result_metadata(ctx.metadata),
        )

        self._store_result(ctx, candidate)
        ctx.status = final_status

    # ------------------------------------------------------------------
    # Input validators
    # ------------------------------------------------------------------

    @staticmethod
    def _require_turn_id(ctx: TurnContext) -> str:
        if ctx.turn_id is None or not ctx.turn_id.strip():
            raise InvalidTurnStateError(
                "turn_id is required before finalization",
                safe_message="Agent turn could not be finalized",
            )
        return ctx.turn_id

    # ------------------------------------------------------------------
    # Status convergence
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_final_status(ctx: TurnContext) -> TurnStatus:
        if ctx.error is not None:
            raise InvalidTurnStateError(
                "cannot finalize a turn containing an error",
                safe_message="Agent turn could not be finalized",
            )

        # Non-standard terminal statuses preserved from AgentLoopPhase
        if ctx.status is TurnStatus.WAITING_APPROVAL:
            return TurnStatus.WAITING_APPROVAL

        if ctx.status is TurnStatus.DENIED:
            return TurnStatus.DENIED

        # Converge RUNNING → COMPLETED
        if ctx.status in (TurnStatus.RUNNING, TurnStatus.COMPLETED):
            return TurnStatus.COMPLETED

        raise InvalidTurnStateError(
            f"cannot finalize turn from status={ctx.status!r}",
            safe_message="Agent turn could not be finalized",
        )

    # ------------------------------------------------------------------
    # Output text resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_output_text(ctx: TurnContext, status: TurnStatus) -> str:
        if status is TurnStatus.WAITING_APPROVAL:
            return ctx.output_text or "该操作需要确认后才能继续。"

        if ctx.output_text is None:
            raise InvalidTurnStateError(
                "output_text is None before finalization",
                safe_message="Agent response is incomplete",
            )
        return ctx.output_text

    # ------------------------------------------------------------------
    # Metadata projection (allowlist only)
    # ------------------------------------------------------------------

    @classmethod
    def _build_result_metadata(
        cls,
        source: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            key: source[key]
            for key in cls._RESULT_METADATA_KEYS
            if key in source
        }

    # ------------------------------------------------------------------
    # Atomic / idempotent result storage
    # ------------------------------------------------------------------

    @staticmethod
    def _store_result(
        ctx: TurnContext,
        candidate: TurnResult,
    ) -> None:
        if ctx.result is None:
            ctx.result = candidate
            return

        # Idempotent: same result on re-entry is OK.
        if ctx.result == candidate:
            return

        # Conflicting result — someone modified ctx after finalization.
        raise InvalidTurnStateError(
            "existing TurnResult conflicts with candidate result",
            safe_message="Agent turn result is inconsistent",
        )
