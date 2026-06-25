# cogito/agent/runtime/phases/turn_init.py

from __future__ import annotations

from dataclasses import dataclass

from cogito.agent.ports.tracing import RuntimeTracePort
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    InvalidAgentRequestError,
    PhaseExecutionError,
)
from cogito.agent.runtime.models import AgentRequest, TurnStatus
from cogito.agent.runtime.phase import BasePhase


@dataclass(frozen=True, slots=True)
class TurnInitConfig:
    """Immutable configuration for TurnInitPhase.

    Attributes:
        max_tool_rounds: Maximum number of model-tool iterations.
        timeout_seconds: Optional per-turn timeout.
    """

    max_tool_rounds: int = 8
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_tool_rounds <= 0:
            raise ValueError(
                f"max_tool_rounds must be greater than zero, got {self.max_tool_rounds}",
            )

        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be greater than zero, got {self.timeout_seconds}",
            )


class TurnInitPhase(BasePhase):
    """Phase 1: Initialize the turn context for safe use by subsequent phases.

    Responsibilities:
      - Validate context identity (turn_id, started_at, status).
      - Validate AgentRequest base integrity.
      - Validate context has no residual state from a previous turn.
      - Set runtime limits (max_tool_rounds, timeout).
      - Start runtime trace.

    Explicitly does NOT:
      - Call Session, Message, Preference, or Memory Repository.
      - Execute keyword/vector retrieval or rerank.
      - Build model_messages.
      - Load tool manifest.
      - Call model or execute tools.
      - Write to database.
      - Generate TurnResult.
      - Send MessageBus Envelope.
      - Read from Channel types.
    """

    name = "turn_init"

    def __init__(
        self,
        *,
        trace: RuntimeTracePort,
        config: TurnInitConfig | None = None,
    ) -> None:
        self._trace = trace
        self._config = config or TurnInitConfig()

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(self, ctx: TurnContext) -> None:
        self._validate_context_identity(ctx)
        self._validate_request(ctx.request)
        self._validate_clean_context(ctx)

        # Set runtime limits
        ctx.max_tool_rounds = self._config.max_tool_rounds

        if self._config.timeout_seconds is not None:
            ctx.metadata["timeout_seconds"] = self._config.timeout_seconds

        # Initialize trace
        try:
            ctx.trace_id = await self._trace.start_turn(
                turn_id=ctx.turn_id,
                request_id=ctx.request.request_id,
            )
        except Exception as exc:
            raise PhaseExecutionError(
                phase=self.name,
                message="Failed to initialize runtime trace",
                safe_message="初始化运行环境失败",
            ) from exc

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_context_identity(ctx: TurnContext) -> None:
        """Verify the context was properly pre-initialized by TurnContextFactory."""
        if not ctx.turn_id:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message="TurnContext.turn_id must be initialized before TurnInitPhase",
                safe_message="初始化运行环境失败",
            )

        if ctx.started_at is None:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message="TurnContext.started_at must be initialized before TurnInitPhase",
                safe_message="初始化运行环境失败",
            )

        if ctx.status is not TurnStatus.RUNNING:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message=f"Unexpected initial turn status: {ctx.status}",
                safe_message="初始化运行状态无效",
            )

    @staticmethod
    def _validate_request(request: AgentRequest) -> None:
        """Validate AgentRequest base integrity.

        Rules:
          - request_id, session_id, actor_id must be non-blank.
          - Either text (after strip) or attachments must be non-empty.
          - Each attachment must have non-blank attachment_id and media_type.
          - No duplicate attachment_id within the same request.
        """
        # Required identifiers
        required_fields = {
            "request_id": request.request_id,
            "session_id": request.session_id,
            "actor_id": request.actor_id,
        }

        for field_name, value in required_fields.items():
            if not value.strip():
                raise InvalidAgentRequestError(
                    f"{field_name} must not be blank",
                    safe_message="请求标识不完整",
                )

        # Content: text or at least one attachment
        if not request.text.strip() and not request.attachments:
            raise InvalidAgentRequestError(
                "Agent request contains neither text nor attachments",
                safe_message="请求内容不能为空",
            )

        # Attachment validation
        attachment_ids: set[str] = set()

        for attachment in request.attachments:
            if not attachment.attachment_id.strip():
                raise InvalidAgentRequestError(
                    "attachment_id must not be blank",
                    safe_message="附件标识无效",
                )

            if not attachment.media_type.strip():
                raise InvalidAgentRequestError(
                    "attachment media_type must not be blank",
                    safe_message="附件类型无效",
                )

            if attachment.attachment_id in attachment_ids:
                raise InvalidAgentRequestError(
                    f"Duplicate attachment_id: {attachment.attachment_id}",
                    safe_message="请求中存在重复附件",
                )

            attachment_ids.add(attachment.attachment_id)

    @staticmethod
    def _validate_clean_context(ctx: TurnContext) -> None:
        """Ensure the context carries no residual state from a previous turn."""
        if ctx.trace_id is not None:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message="Trace has already been initialized",
                safe_message="本轮运行状态重复初始化",
            )

        dirty_fields = {
            "retrieved_items": bool(ctx.retrieved_items),
            "model_messages": bool(ctx.model_messages),
            "model_responses": bool(ctx.model_responses),
            "tool_records": bool(ctx.tool_records),
            "preference_candidates": bool(ctx.preference_candidates),
            "memory_candidates": bool(ctx.memory_candidates),
            "output_text": ctx.output_text is not None,
            "result": ctx.result is not None,
            "persistence_completed": ctx.persistence_completed,
        }

        dirty = [name for name, populated in dirty_fields.items() if populated]

        if dirty:
            raise PhaseExecutionError(
                phase=TurnInitPhase.name,
                message=f"TurnContext is not clean: {', '.join(dirty)}",
                safe_message="本轮运行状态无效",
            )
