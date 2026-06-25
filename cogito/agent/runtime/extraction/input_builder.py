# cogito/agent/runtime/extraction/input_builder.py
#
# ExtractionInputBuilder — builds a safe, trimmed KnowledgeExtractionInput
# from TurnContext.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.3):
#   - Never includes full history, full system prompt, tool secrets, or
#     unrelated retrieved results.
#   - Input is trimmed to config limits (max_user_text_chars,
#     max_assistant_text_chars).

from __future__ import annotations

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
from cogito.agent.domain.knowledge.extraction import KnowledgeExtractionInput
from cogito.agent.domain.preferences import PreferenceCandidate
from cogito.agent.runtime.context import TurnContext


class ExtractionInputBuilder:
    """Build a trimmed KnowledgeExtractionInput from TurnContext."""

    def __init__(self, config: KnowledgeExtractionConfig) -> None:
        self._config = config

    def build(self, ctx: TurnContext) -> KnowledgeExtractionInput:
        """Build extraction input from the turn context.

        Args:
            ctx: The current turn context (must have output_text set).

        Returns:
            A validated, trimmed KnowledgeExtractionInput.
        """
        user_text = (ctx.request.text or "")[: self._config.max_user_text_chars]
        assistant_text = (ctx.output_text or "")[: self._config.max_assistant_text_chars]

        current_prefs = tuple(
            p
            for p in getattr(ctx, "current_preferences", [])
            if isinstance(p, PreferenceCandidate)
        )

        return KnowledgeExtractionInput(
            turn_id=ctx.turn_id or "",
            request_id=ctx.request.request_id,
            actor_id=ctx.request.actor_id,
            session_id=ctx.request.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            current_preferences=current_prefs,
            locale=(
                ctx.user_settings.locale
                if hasattr(ctx, "user_settings") and ctx.user_settings
                else None
            ),
        )
