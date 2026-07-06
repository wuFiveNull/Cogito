# cogito/agent/runtime/summarizer.py
#
# DefaultSummarizer — wraps ModelPort to produce LLM-based summaries.
#
# Constructs a minimal ModelInvocationRequest with a summarisation
# prompt, stream-calls the model, and assembles the text output.
#
# Error behaviour: summarization failures are always non-fatal.
# The summarizer catches all exceptions and returns a fallback
# string (empty or the original truncated text).

from __future__ import annotations

import logging
from contextlib import aclosing

from cogito.agent.domain.messages import SystemMessage, UserMessage
from cogito.agent.domain.model import (
    ModelCompleted,
    ModelFinishReason,
    ModelInvocationRequest,
    ModelStreamEvent,
    ModelTextDelta,
)
from cogito.agent.ports.model import ModelPort

logger = logging.getLogger(__name__)

# Default system prompt for the summarizer
SUMMARIZATION_SYSTEM_PROMPT = """You are a conversation summarizer. Your task is to produce a concise summary of the conversation so far.

Rules:
1. Keep the summary under 300 words.
2. Focus on: user goals, key decisions, resolved questions, and any important context that would be needed to continue.
3. Do NOT include conversational filler, greetings, or meta-commentary.
4. Write in the same language as the user's messages.
5. When updating an existing summary, merge the new content with the old — do not repeat information unless it is essential."""


class DefaultSummarizer:
    """LLM-based summarizer wrapping a ModelPort.

    Args:
        model: The ModelPort to use for generation.
        model_name: Human-readable model name for telemetry.
        system_prompt: Override the default summarisation prompt.
    """

    def __init__(
        self,
        model: ModelPort,
        *,
        model_name: str = "summarizer",
        system_prompt: str | None = None,
    ) -> None:
        self._model = model
        self._model_name = model_name
        self._system_prompt = system_prompt or SUMMARIZATION_SYSTEM_PROMPT

    async def summarize(
        self,
        *,
        text: str,
        existing_summary: str | None = None,
        max_output_tokens: int = 512,
        timeout_seconds: float = 30.0,
    ) -> str:
        """Produce or update a summary using the LLM.

        Falls back to truncation on any error.
        """
        if not text.strip():
            return existing_summary or ""

        user_prompt = self._build_prompt(text, existing_summary)
        messages = (
            SystemMessage(content=self._system_prompt),
            UserMessage(content=user_prompt),
        )

        request = ModelInvocationRequest(
            turn_id="summary",
            request_id="summary",
            round_index=0,
            messages=messages,
            tools=(),
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )

        try:
            result = await self._invoke_summary(request)
            if result and result.strip():
                logger.debug(
                    "Summarization succeeded (%d chars, model=%s)",
                    len(result),
                    self._model_name,
                )
                return result.strip()
        except Exception as exc:
            logger.warning(
                "Summarization failed (model=%s): %s",
                self._model_name,
                exc,
            )

        # Fallback: return truncated original text
        fallback = text[:1000]
        if existing_summary:
            fallback = f"{existing_summary}\n[truncated: {fallback}]"
        return fallback

    def _build_prompt(
        self,
        text: str,
        existing_summary: str | None,
    ) -> str:
        if existing_summary:
            return (
                f"当前对话摘要：\n{existing_summary}\n\n"
                f"请将以下新对话内容合并到现有摘要中：\n\n{text}"
            )
        return f"请总结以下对话内容：\n\n{text}"

    async def _invoke_summary(
        self,
        request: ModelInvocationRequest,
    ) -> str | None:
        """Stream the model and collect text output."""
        collected: list[str] = []

        async with aclosing(self._model.stream(request)) as stream:
            async for event in stream:
                if isinstance(event, ModelTextDelta):
                    collected.append(event.text)
                elif isinstance(event, ModelCompleted):
                    if event.finish_reason in (
                        ModelFinishReason.STOP,
                        ModelFinishReason.LENGTH,
                    ):
                        break
                    # Other finish reasons → discard partial output
                    return None

        return "".join(collected) if collected else None
