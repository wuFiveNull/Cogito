# cogito/agent/ports/summarizer.py
#
# SummarizerPort — LLM-based summarization for context compression.
#
# Designed for two use cases:
#   1. Iterative summarization (Mode 4) — called from the context overflow
#      handler when hard governance steps are insufficient.
#   2. Idle session compression (Mode 11) — background summarization of
#      old session messages.
#
# The port is intentionally simple: text in, text out.  All formatting
# and prompt construction is the caller's responsibility.

from __future__ import annotations

from typing import Protocol


class SummarizerPort(Protocol):
    """Abstract summarization interface backed by an LLM."""

    async def summarize(
        self,
        *,
        text: str,
        existing_summary: str | None = None,
        max_output_tokens: int = 512,
        timeout_seconds: float = 30.0,
    ) -> str:
        """Summarise the given text.

        When ``existing_summary`` is provided, the summarizer should
        produce an updated summary that incorporates both the existing
        summary and the new text (iterative mode).
        """
        ...
